# crema ☕

Automated GaggiMate shot-history reviewer. Pulls your recent shots off the
machine, sends their telemetry to Claude, and gets back concrete grind / dose /
profile suggestions — in a web report you can also trigger on demand.

Designed to run on an always-on Raspberry Pi on the same LAN as the machine. See
[`SCOPE.md`](./SCOPE.md) for the full architecture and roadmap.

## What it does

- **Ingest** — downloads new shots (binary `.slog`) from the device and parses
  them into AI-friendly JSON with physics diagnostics (channeling risk,
  puck resistance, temp stability, profile compliance).
- **Review** — hands the recent-shot window to Claude and stores a structured
  set of suggestions.
- **Draft** — turns a review's profile suggestions into a complete, validated
  profile (rewritten from the one the shot ran on), clamped to device-safe
  bounds, stored as a *pending edit*.
- **Approve & push** — on your say-so, writes the edit to the machine as a **new
  `[AI]` profile** over WebSocket (never overwrites your original; you select it
  on the machine).
- **Report** — a small web page shows reviews, drafts, and recent shots, with
  “Run review”, “Draft profile edit”, and “Approve & push” buttons.

Grind and dose/yield are suggested as text (manual bench changes); only profile
changes get drafted/pushed. See `SCOPE.md` for the roadmap.

## Reuse

The binary parsing and device API clients are vendored from the MIT-licensed
[`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) project under
`src/gaggimate_mcp/` (see its `_vendor_meta/`), so crema doesn't reimplement the
`.slog` format.

## License & credits

crema is released under the [MIT License](./LICENSE).

Credits:
- [`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) by
  julianleopold (MIT) — vendored for the `.slog` parser, device HTTP/WebSocket
  clients, and shot transformer.
- [GaggiMate](https://gaggimate.eu/) by jniebuhr — the open-source smart-controller
  project this works with. "GaggiMate" and "Gaggia" are used descriptively; crema
  is an independent project and is not affiliated with or endorsed by either.

## Setup

Requires Python 3.13+. On the Pi:

```bash
cp .env.example .env      # then edit: ANTHROPIC_API_KEY + GAGGIMATE_GAGGIMATE_HOST
uv sync                   # or: pip install -e .
```

Confirm the Pi can reach the machine (`ping gaggimate.local`), then:

```bash
crema doctor              # check device + Claude connectivity
crema ingest              # pull new shots (also prunes shots past the retention window)
crema review              # ingest + review (auto-gated: only new shots, only if autoreview on)
crema analyze SHOT_ID     # review one specific shot
crema draft [REVIEW_ID]   # draft a profile edit (--profile-id to target a specific profile)
crema edits               # list drafted / pushed edits
crema push EDIT_ID        # approve & push an edit to the machine as a new [AI] profile
crema discard EDIT_ID     # discard a drafted edit
crema autoreview [on|off] # toggle automatic review of new shots by the timer
crema serve               # web report at http://127.0.0.1:8765
```

The web report shows machine online/off status, the latest review (with a 1–10
score sent to Discord), drafted edits, and recent shots — with buttons to run a
review, analyze a specific shot, draft an edit for a chosen profile, approve/push
edits, and toggle auto-review.

## Configuration

All via `.env` (see `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (read by the SDK) | — |
| `GAGGIMATE_GAGGIMATE_HOST` | machine hostname/IP on the LAN | `gaggimate.local` |
| `CREMA_REVIEW_MODEL` | model for routine reviews | `claude-sonnet-5` |
| `CREMA_DRAFT_MODEL` | model for profile drafting (Phase 2) | `claude-opus-4-8` |
| `CREMA_REVIEW_WINDOW` | shots per review | `5` |
| `CREMA_RETENTION_DAYS` | prune shots older than this (0 = keep all) | `30` |
| `CREMA_AUTOREVIEW` | default for timer auto-review (UI toggle overrides) | `false` |
| `CREMA_DISCORD_WEBHOOK_URL` | Discord webhook for shot score notifications | — |
| `CREMA_WEB_USER` / `CREMA_WEB_PASSWORD` | web Basic auth (blank pw = open) | `crema` / — |
| `CREMA_DB_PATH` | SQLite path | `./crema.db` |
| `CREMA_HOST` / `CREMA_PORT` | web bind | `127.0.0.1` / `8765` |

The web UI binds to loopback by default. To reach it off-network, put a
Cloudflare Tunnel (or similar) in front of *just the UI* — don't bind `0.0.0.0`
without auth.

## Cost

crema only calls the paid Claude API when there's **a new shot to review**. The
scheduled timer ingests every 15 min but skips the review unless a new shot came
in, and the web "Run review" button does the same — so you pay per shot you
actually pull, not per timer tick. A review is roughly a few cents; `crema review`
prints the token count each time so you can see it.

Levers if you want it cheaper: lower `CREMA_REVIEW_WINDOW` (fewer shots per
review = fewer input tokens), or keep `CREMA_REVIEW_MODEL=claude-sonnet-5` (the
cheap default) rather than an Opus model. Realistic use (a handful of shots a day)
lands around a dollar or two a month, not a week.

## Scheduling

Run `crema review` on a timer. Simplest is a systemd timer or cron on the Pi,
e.g. every 15 minutes:

```
*/15 * * * * cd /home/pi/crema && /home/pi/crema/.venv/bin/crema review >> crema.log 2>&1
```
