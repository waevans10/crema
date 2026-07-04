# crema ☕

**Automated GaggiMate shot-history reviewer.** crema pulls your recent espresso
shots off the machine, sends their telemetry to Claude, and gets back concrete
grind / dose / profile suggestions — in a small web report you can also trigger
on demand. It runs unattended on an always-on box (a Raspberry Pi is ideal) on
the same network as the machine.

Think of it as the hands-off counterpart to the interactive
[`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) server: instead
of opening a chat to ask Claude about your shots, crema watches for new shots and
reviews them automatically, then shows the result in a web page with a one-click
path to draft and push a corrected profile.

## Screenshots

<!-- Drop a screenshot of the web report at docs/report.png and uncomment: -->
<!-- ![crema web report](docs/report.png) -->

_Add a screenshot of the web report here — see [`docs/SCREENSHOTS.md`](docs/SCREENSHOTS.md) for how to capture one._

## What you need

- A **GaggiMate**-controlled espresso machine, reachable on your home network
  (its API is LAN-only — see [How it works](#how-it-works)).
- An **always-on computer on the same network** to run crema. A Raspberry Pi is
  the intended target, but any always-on Linux/macOS box works.
- **Python 3.13+**.
- A **paid Anthropic API account** for Claude — this is what does the reviewing
  (see [Which LLM](#which-llm)). **This costs real money: you pay Anthropic
  directly, per shot reviewed.** It's cheap for home use (roughly a dollar or two
  a month), but it is a metered bill, not a free service — read [Cost](#cost)
  before you set this up.

You do **not** need the machine powered on to browse past reviews or draft a
profile edit — only to pull new shots or push an approved edit back.

## What it does

- **Ingest** — downloads new shots (binary `.slog`) from the device and parses
  them into AI-friendly JSON with physics diagnostics (channeling risk,
  puck resistance, temp stability, profile compliance).
- **Review** — hands the recent-shot window to Claude and stores a structured
  set of suggestions plus a 1–10 quality score.
- **Draft** — turns a review's profile suggestions into a complete, validated
  profile (rewritten from the one the shot ran on), clamped to device-safe
  bounds, stored as a *pending edit*.
- **Approve & push** — on your say-so, writes the edit to the machine as a **new
  `[AI]` profile** over WebSocket (never overwrites your original; you select it
  on the machine).
- **Report** — a small web page shows reviews, drafts, and recent shots, with
  “Run review”, “Draft profile edit”, and “Approve & push” buttons.

Grind and dose/yield are suggested as text (manual bench changes); only profile
changes get drafted and pushed.

## How it works

The GaggiMate machine only exposes its API on your **home network** (list/
download shots over HTTP, read/write profiles over WebSocket). So a scheduled
reviewer needs an always-on box on that same network — the Pi that's already
running is exactly that. Everything runs there and reaches the machine directly;
no cloud, no tunnel-to-device.

```
        always-on box (e.g. a Pi) on your LAN
 ┌────────────┐   ┌─────────────────────────────────────────┐
 │  GaggiMate │   │  timer / "Run review" button            │
 │  machine   │◀──│    │                                    │
 │ HTTP + WS  │   │    ├─ pull new shots → parse .slog       │
 └────────────┘   │    ├─ send recent shots → Claude review  │
   direct LAN     │    ├─ store shots + reviews (SQLite)     │
   access         │    └─ web report ── "Approve & push" ────┼──▶ new [AI]
                  └─────────────────────────────────────────┘    profile on
                                                                  the machine
```

To spend as little as possible, Claude is only called when there's a **new shot
to review** — the timer ingests on a schedule but skips the review unless
something new arrived. See [Cost](#cost).

## How crema is different

The closest existing projects are **interactive**: the
[`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) server lets you
chat with an LLM about your shots, and hosted services generate profiles from a
bean description. crema fills a different niche — **unattended and self-hosted**:

- It runs on a timer and reviews new shots on its own; you don't open a chat.
- Everything lives on your own box; no shot data leaves your network except the
  telemetry sent to Claude for the review itself.
- It's cost-gated by design (a review only happens on a genuinely new shot).
- Profile changes are always **suggest → draft → you approve → push**, and a
  push creates a new `[AI]` profile, so your originals are never touched.

## Quickstart

Requires Python 3.13+. From the project directory:

```bash
cp .env.example .env      # then edit: ANTHROPIC_API_KEY + GAGGIMATE_GAGGIMATE_HOST
uv sync                   # or: pip install -e .
```

Set `GAGGIMATE_GAGGIMATE_HOST` to the machine's address. **Recommended:** give it
a fixed IP with a DHCP reservation on your router (a plain DHCP address changes on
reconnect and crema will lose the machine). `gaggimate.local` works with zero
config on many networks, but not all — and never across subnets. Confirm the box
can reach it (`ping gaggimate.local`, or `ping <the IP>`), then:

```bash
crema doctor              # check device + Claude connectivity
crema ingest              # pull new shots (also prunes shots past the retention window)
crema review              # ingest + review (only spends on new shots, only if autoreview on)
crema serve               # web report at http://127.0.0.1:8765
```

Open the report, click **Run review**, and you'll get a scored review with
suggestions. To run crema unattended on a Raspberry Pi (systemd units, a
LAN-bound password-protected web report), see
[`deploy/PI_SETUP.md`](deploy/PI_SETUP.md) — or run the one-shot
[`deploy/setup.sh`](deploy/setup.sh) from the project root.

### All commands

```bash
crema doctor              # check device + Claude connectivity
crema ingest              # pull new shots (also prunes shots past the retention window)
crema review [--force]    # ingest + review (auto-gated: only new shots, only if autoreview on)
crema analyze SHOT_ID     # review one specific shot
crema draft [REVIEW_ID]   # draft a profile edit (--profile-id to target a specific profile)
crema edits               # list drafted / pushed edits
crema push EDIT_ID        # approve & push an edit to the machine as a new [AI] profile
crema discard EDIT_ID     # discard a drafted edit
crema autoreview [on|off] # toggle automatic review of new shots by the timer
crema serve               # web report at http://127.0.0.1:8765
```

## Which LLM

crema is built on the **Anthropic SDK** and uses **Claude** by default — it's the
recommended choice and what the project is tuned and tested against. Two things
depend on Claude specifically:

- **Structured output** — reviews and drafts come back as validated JSON via
  Claude's `messages.parse`, so the app can render and act on them safely.
- **Reasoning quality on the physics** — diagnosing an extraction from
  temperature/pressure/flow curves is exactly the kind of task the recommended
  models are strong at. Routine reviews run on the cheaper `claude-sonnet-5`;
  the occasional profile draft uses the stronger `claude-opus-4-8`.

**Prefer a different model?** The model IDs are just `.env` settings
(`CREMA_REVIEW_MODEL`, `CREMA_DRAFT_MODEL`), so you can point them at any current
Claude model. Swapping to a *non-Claude* provider is a small code change rather
than a config toggle: the two API calls live in
[`src/crema/review.py`](src/crema/review.py) and
[`src/crema/draft.py`](src/crema/draft.py) — both use the Anthropic client with a
Pydantic-typed structured response. Replace those two calls with your provider's
structured-output equivalent and the rest of crema is unchanged.

### Running it for free (lower quality)

There's no free Claude tier — the Anthropic API is paid. If you want **zero API
cost**, the only route is to point those two calls at a free model instead, and
accept that the reviews get less sharp. This is a DIY code change (crema ships
Claude-only), but the seam is small and lives entirely in `review.py` and
`draft.py`. Two options:

- **Local model with [Ollama](https://ollama.com/)** — run a small open model
  (e.g. Llama 3.x 8B, Qwen 2.5 7B) on the same box. Truly free and fully private
  (nothing leaves your network). Caveats: it needs a **64-bit machine with a few
  GB of RAM** — a mini PC, spare laptop, a Mac, or a Pi 5 (8GB+); the small
  32-bit Pi this was built on **can't** run it. And quality drops most exactly
  where it matters — reasoning about the extraction physics and reliably
  producing valid profile JSON.
- **A free hosted tier** such as **Google Gemini (Flash)** or **[Groq](https://groq.com/)** —
  both have genuine free tiers (with rate limits) and support structured JSON
  output. Better reasoning than a small local model and no hardware needed, but
  your shot telemetry goes to that provider, and free limits can throttle or
  change — check each provider's current terms.

In both cases you keep the same Pydantic result shapes (`ReviewResult`,
`DraftedProfile` in [`src/crema/prompts.py`](src/crema/prompts.py)) — you're only
changing which client produces them. Expect vaguer diagnoses and the occasional
malformed draft on the cheapest models; the paid Claude default is what the
prompts are tuned and tested against.

## Configuration

All via `.env` (see [`.env.example`](.env.example)):

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (read by the SDK) | — |
| `GAGGIMATE_GAGGIMATE_HOST` | machine hostname/IP on the LAN | `gaggimate.local` |
| `CREMA_REVIEW_MODEL` | model for routine reviews | `claude-sonnet-5` |
| `CREMA_DRAFT_MODEL` | model for the deeper profile-drafting step | `claude-opus-4-8` |
| `CREMA_REVIEW_WINDOW` | shots per review | `5` |
| `CREMA_RETENTION_DAYS` | prune shots older than this (0 = keep all) | `30` |
| `CREMA_AUTOREVIEW` | default for timer auto-review (UI toggle overrides) | `false` |
| `CREMA_DISCORD_WEBHOOK_URL` | Discord webhook for shot score notifications | — |
| `CREMA_WEB_USER` / `CREMA_WEB_PASSWORD` | web Basic auth (blank pw = open) | `crema` / — |
| `CREMA_DB_PATH` | SQLite path | `./crema.db` |
| `CREMA_HOST` / `CREMA_PORT` | web bind | `127.0.0.1` / `8765` |

The web UI binds to loopback by default. To reach it off-network, put a
Cloudflare Tunnel (or similar) in front of *just the UI* — don't bind `0.0.0.0`
without a password.

## Cost

> [!IMPORTANT]
> **crema uses the paid Anthropic API. Running it puts real charges on your own
> Anthropic account** — you set up billing with Anthropic and pay them directly
> for every review. There is no free tier that covers this, and crema doesn't
> bundle any credits. It's inexpensive for normal home use, but you should
> understand that reviewing shots costs money before you start.

The good news is that it's designed to stay cheap. crema only calls Claude when
there's **a new shot to review**: the scheduled timer ingests every 15 min but
**skips** the review unless a new shot came in, and the web “Run review” button
does the same — so you pay per shot you actually pull, not per timer tick. A
single review is roughly **a few cents**, and `crema review` prints the token
count each time so you can watch what you're spending.

**Ballpark:** a handful of shots a day lands around **a dollar or two a month**.
Heavy use (many shots a day, a large review window, the Opus model on every
review) costs more — it scales with how much you review.

**Nothing spends automatically until you opt in.** Auto-review is **off** by
default (`CREMA_AUTOREVIEW=false`), so the timer won't call the API on its own —
reviews only happen when you press “Run review” (or run `crema review`) until you
deliberately turn auto-review on. Levers if you want it cheaper: lower
`CREMA_REVIEW_WINDOW` (fewer shots per review = fewer input tokens), or keep
`CREMA_REVIEW_MODEL=claude-sonnet-5` (the cheap default) rather than an Opus
model. You can also set a spend limit on your key in the
[Anthropic Console](https://console.anthropic.com/) as a hard backstop.

## Scheduling

On a Pi, [`deploy/setup.sh`](deploy/setup.sh) installs a systemd timer that runs
`crema review` every 15 minutes plus an always-on web service — see
[`deploy/PI_SETUP.md`](deploy/PI_SETUP.md). If you'd rather use cron:

```
*/15 * * * * cd /home/pi/crema && /home/pi/crema/.venv/bin/crema review >> crema.log 2>&1
```

## Reuse

The binary parsing and device API clients are vendored from the MIT-licensed
[`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) project under
`src/gaggimate_mcp/` (see its `_vendor_meta/`), so crema doesn't reimplement the
`.slog` format.

## Contributing

Issues and pull requests are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md)
for how to run the tests and the layout of the code.

## Support ☕

crema is free and open source. If it saves you a few bad shots and you'd like to
chip in toward the Claude API costs, you can
[buy me a coffee](https://www.buymeacoffee.com/waevans10f) — entirely optional,
and genuinely just to cover costs. There's nothing behind a paywall.

## License & credits

crema is released under the [MIT License](./LICENSE).

Credits:
- [`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) by
  julianleopold (MIT) — vendored for the `.slog` parser, device HTTP/WebSocket
  clients, and shot transformer.
- [GaggiMate](https://gaggimate.eu/) by jniebuhr — the open-source smart-controller
  project this works with. “GaggiMate” and “Gaggia” are used descriptively; crema
  is an independent project and is not affiliated with or endorsed by either.
