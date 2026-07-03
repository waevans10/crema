# crema ☕

Automated GaggiMate shot-history reviewer. Pulls your recent shots off the
machine, sends their telemetry to Claude, and gets back concrete grind / dose /
profile suggestions — in a web report you can also trigger on demand.

Designed to run on an always-on Raspberry Pi on the same LAN as the machine. See
[`SCOPE.md`](./SCOPE.md) for the full architecture and roadmap.

## What it does (Phase 1)

- **Ingest** — downloads new shots (binary `.slog`) from the device and parses
  them into AI-friendly JSON with physics diagnostics (channeling risk,
  puck resistance, temp stability, profile compliance).
- **Review** — hands the recent-shot window to Claude and stores a structured
  set of suggestions.
- **Report** — a small read-only web page shows the latest review and recent
  shots, with a “Run review” button.

Writing adjusted profiles back to the machine (the “draft profile edit” button)
is Phase 2 — see `SCOPE.md`.

## Reuse

The binary parsing and device API clients are vendored from the MIT-licensed
[`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) project under
`src/gaggimate_mcp/` (see its `_vendor_meta/`), so crema doesn't reimplement the
`.slog` format.

## Setup

Requires Python 3.13+. On the Pi:

```bash
cp .env.example .env      # then edit: ANTHROPIC_API_KEY + GAGGIMATE_GAGGIMATE_HOST
uv sync                   # or: pip install -e .
```

Confirm the Pi can reach the machine (`ping gaggimate.local`), then:

```bash
crema ingest              # pull new shots
crema review              # ingest + run a Claude review, print suggestions
crema serve               # web report at http://127.0.0.1:8765
```

## Configuration

All via `.env` (see `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (read by the SDK) | — |
| `GAGGIMATE_GAGGIMATE_HOST` | machine hostname/IP on the LAN | `gaggimate.local` |
| `CREMA_REVIEW_MODEL` | model for routine reviews | `claude-sonnet-5` |
| `CREMA_DRAFT_MODEL` | model for profile drafting (Phase 2) | `claude-opus-4-8` |
| `CREMA_REVIEW_WINDOW` | shots per review | `5` |
| `CREMA_DB_PATH` | SQLite path | `./crema.db` |
| `CREMA_HOST` / `CREMA_PORT` | web bind | `127.0.0.1` / `8765` |

The web UI binds to loopback by default. To reach it off-network, put a
Cloudflare Tunnel (or similar) in front of *just the UI* — don't bind `0.0.0.0`
without auth.

## Scheduling

Run `crema review` on a timer. Simplest is a systemd timer or cron on the Pi,
e.g. every 15 minutes:

```
*/15 * * * * cd /home/pi/crema && /home/pi/crema/.venv/bin/crema review >> crema.log 2>&1
```
