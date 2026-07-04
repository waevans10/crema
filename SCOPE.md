# Espresso Shot Auto-Reviewer — Scope

An automated + interactive system that reviews GaggiMate shot history with Claude and
suggests adjustments to grind size, dose/yield, and profile — with a one-click path to
draft a corrected profile for approval and push it back to the machine.

## Decisions locked in

| Decision | Choice |
|---|---|
| Trigger | **Both** — scheduled/auto batch reviews *and* on-demand interactive chat |
| Autonomy | **Suggest first**, then a button to **draft a profile edit for approval** (no silent auto-apply) |
| Foundation | **Reuse `julianleopold/gaggimate-mcp`** internals (proven `.slog` parser + device API client) |
| Runtime | **Self-hosted on the always-on Raspberry Pi** (on the LAN), scheduled via cron/systemd |

## The connectivity constraint (why the architecture looks like this)

The GaggiMate machine only exposes its API on your **home LAN**:
- `http://<ip>/api/` — list shot history, download binary `.slog` files
- `ws://gaggimate.local/ws` — read/write profiles, shot notes

A scheduled reviewer therefore needs an **always-on box on the same LAN**. The Pi that's
already running is exactly that — so everything runs on the Pi and reaches the machine
directly. No tunnel-to-device, no cloud compute.

## Chosen architecture — all-on-Pi

**One Python service on the Raspberry Pi**, reusing `gaggimate-mcp` top-to-bottom
(parser + HTTP + WS client). Because the Pi is on the LAN, the "Approve & push" button
writes profiles straight to the machine over WS — no tunnel needed for the core loop.

```
                 RASPBERRY PI (always-on, on the LAN)
 ┌──────────────┐   ┌────────────────────────────────────────────┐
 │  GaggiMate   │   │  systemd timer / cron  ──▶ Review job       │
 │  ESP32       │   │        │                                    │
 │  HTTP + WS   │◀──│  gaggimate-mcp core:                        │
 └──────────────┘   │    • HTTP client → list/download shots      │
   direct LAN       │    • .slog parser → AI-friendly JSON        │
   access           │    • WS client → read/write profiles        │
                    │        │                                    │
                    │        ▼                                    │
                    │   Claude API (Anthropic SDK)                │
                    │        │  review → suggestions              │
                    │        ▼                                    │
                    │   SQLite (shots, reviews, grind map,        │
                    │           pending profile edits)            │
                    │        │                                    │
                    │        ▼                                    │
                    │   Web UI ── "Draft profile edit" button ────┼──▶ push
                    └──────────────────┬─────────────────────────┘   over WS
                                       │                              to machine
                                       │ (optional) cloudflared
                                       ▼  exposes ONLY the web UI
                              remote phone access away from home
```

**Remote access is optional and additive:** one `cloudflared` tunnel exposing just the
web UI lets you read reviews / hit the approve button from your phone off-network.
Skip it to start — LAN-only is fine. Storage is local **SQLite** on the Pi.

### Alternatives considered
- **Cloud review service + tunnel to the device.** Genuinely cloud-hosted, but requires
  a `cloudflared` bridge into the LAN and adds a second host. The Pi makes this
  unnecessary. Rejected: more moving parts for no benefit here.
- **Serverless Worker (Cloudflare) doing everything.** True serverless + built-in cron,
  but the `.slog` parser is Python — you'd rewrite it in TS. Loses the main reuse win.
  Rejected.

## Components to build

### 1. Review service (Python, on the Pi) — the bulk of the work
Wraps `gaggimate-mcp` internals rather than the MCP protocol layer:
- **Ingest** — poll device HTTP API for new shots, download `.slog`, parse to JSON
  (reuse `parsers/shot.py` + `transformers/shot.py`), store. Track "last seen" shot id.
- **Scheduler** — cron (APScheduler or the platform's cron) to run reviews:
  per-shot on new data + a rollup (e.g. daily/weekly trend).
- **Reviewer** — build a Claude prompt from recent shots + current profile + bean/grind
  context; get structured suggestions back. See "Claude review" below.
- **Profile drafter** — on button click, ask Claude to emit a *complete valid profile
  JSON* implementing the suggested changes; validate against the profile schema; store
  as a **pending edit** (never auto-pushed).
- **Approval + push** — UI approve → write profile to device over WS (reuse
  `api/websocket.py`), tagged so it's traceable (e.g. `[AI]` label like gaggimate-mcp).
- **Interactive chat** — a chat endpoint that gives Claude the same read tools
  (list/analyze shots, read profiles) for "review my last 3 shots" style questions.
  Can reuse the gaggimate-mcp MCP server directly for this half.

### 2. (Optional) remote access
- One `cloudflared` tunnel exposing **only the web UI** for off-network phone access.
- Not needed for the core loop; add later if you want it.

### 3. Web UI
- Shot list + latest review report (grind/dose/yield/profile suggestions, with the
  "why").
- **"Draft profile edit"** button per suggestion → shows generated profile diff →
  **Approve & push** / discard.
- Grind map + bean tracking views (gaggimate-mcp already models these).

## Data model (minimal)
- `shots` — id, timestamp, bean id, grind setting, dose, yield, parsed metrics JSON,
  raw `.slog` blob ref.
- `reviews` — shot id (or range), model used, suggestions JSON, rating band, created_at.
- `profiles` — cached device profiles + versions.
- `pending_edits` — proposed profile JSON, source review id, status
  (draft/approved/pushed/discarded).
- `grind_map` / `beans` — carry over from gaggimate-mcp's local storage models.

## Claude review design
**Inputs per review:** last N shots' parsed metrics (temp/pressure/flow curves +
diagnostics like channeling risk, puck resistance, temp stability), the profile used,
dose/yield/time, bean info, and recent grind-map history. **Output:** structured JSON —
`{ diagnosis, grind_change, dose_yield_change, profile_changes[], confidence, rationale }`
— so the UI can render it and the drafter can act on `profile_changes`.

**Models:** default routine per-shot reviews to **`claude-sonnet-5`** (cheap, frequent);
escalate the weekly rollup and the "draft a new profile" step to **`claude-opus-4-8`**
for deeper reasoning. All via the Anthropic SDK.

**Safety:** reuse gaggimate-mcp's guardrails — temp 25–100 °C, pressure 0–12 bar, AI only
edits/deletes profiles it created, never triggers a shot. Pending edits always require
human approval before a WS push.

## Suggested build phases
1. **Read-only slice:** service on the Pi — ingest + parse shots, run a review, render
   report in UI. No writes to the machine. Proves the whole pipe end-to-end.
2. **Draft + approve + push:** profile drafter, pending-edit review UI, WS push-back.
3. **Scheduling + rollups:** cron triggers, per-shot vs weekly-trend reviews, grind map.
4. **Interactive chat:** wire the gaggimate-mcp MCP server for live Q&A.

## Resolved

The questions that shaped the build, and how they landed:

- **Pi details** — runs on a Raspberry Pi (armv7 / Raspberry Pi OS) with Python 3.13
  installed via `uv` (Pi OS ships 3.11). The machine is reached by fixed IP; mDNS
  (`gaggimate.local`) proved flaky.
- **Anthropic API key + budget** — Claude via the Anthropic SDK, cost-gated to a
  dollar or two a month (a review only runs on a genuinely new shot). Routine
  reviews on `claude-sonnet-5`, profile drafting on `claude-opus-4-8`.
- **Fork vs. vendor** — vendored `gaggimate-mcp`'s parser/client modules into
  `src/gaggimate_mcp/` (see `_vendor_meta/`), so the `.slog` format isn't
  reimplemented.
- **Remote access** — LAN-only to start; an optional `cloudflared` tunnel over just
  the web UI can be added later for off-network access.
```
