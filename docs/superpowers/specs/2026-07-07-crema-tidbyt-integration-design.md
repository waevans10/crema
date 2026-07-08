# crema Tidbyt integration — design

**Date:** 2026-07-07
**Repo:** `Coffee` (crema, open source)
**Status:** design — awaiting review

## Goal

Give crema an **opt-in Tidbyt integration** so a user's Tidbyt pixel display shows
the latest reviewed shot: its **1–10 score** and the **profile name** it ran on.
The frame updates **when the LLM finishes a review** (a new score exists), not on
every raw shot ingest.

This is a community feature that ships in crema for anyone with a Tidbyt, not a
one-off for a single kitchen. A companion project (`~/Documents/KitchenTidbyt`)
handles a separate BeltwayIQ app and is out of scope here.

## Non-goals

- No new always-on service or polling loop. The push rides the existing
  review-completion event.
- No trends/history on the display — one shot, one glance. (Score history already
  lives on the `/trends` web page.)
- No device-secret storage beyond env vars, consistent with crema's other secrets.
- Not building the BeltwayIQ app or any shared "kitchen orchestrator" here.

## Integration point

crema already notifies on review completion via `src/crema/notify.py`
(`notify_review(config, review)` → Discord embed; best-effort, no-op unless
`CREMA_DISCORD_WEBHOOK_URL` is set). The Tidbyt push is **another channel on the
same event**: a new `push_review`-style call fired right after a review is stored,
next to the existing Discord call, guarded by its own config and swallowing all
errors so a display problem never breaks a review.

## Data contract

The frame needs, per latest review:

| Field | Source |
|---|---|
| `score` (1–10 int) | `review["suggestions"]["score"]` (clamped 1–10, same as notify.py) |
| `profile_name` | the reviewed shot's `transformed["profile_name"]` — present for **both** GaggiMate and Gaggiuino (`transform_shot_for_ai` emits it; Gaggiuino maps `profile.name` → `profile_name`) |
| `bean` (optional, for context line) | the reviewed shot's `coffee` string (`shots[0]["coffee"]`) |

**Resolved (was open at spec draft):** `profile_name` is uniform across both device
types — no per-device branching needed. `review.py` enriches the `stored` dict with
`profile_name` + `bean` from `shots[0]` (already in scope) so the push helper keeps
notify.py's `(config, review)` interface and needs no DB handle.

## Components

New module `src/crema/tidbyt.py`, plus config keys. No `.star` file and no
`pixlet` binary (see Rendering approach).

1. **`src/crema/tidbyt.py`** — two responsibilities:
   - `render_frame(score, profile, bean, stale) -> bytes` — draws a 64×32 WebP with
     Pillow: large score number (color-coded red<4 / amber<7 / green≥7, matching
     notify.py's `_score_color`), the profile name below in a small font wrapped to
     two lines and ellipsized if long, a dim corner dot when `stale`. Pure/offline,
     easily unit-tested.
   - `push_review(config, review)` — no-op unless Tidbyt config is set; renders the
     frame, base64-encodes it, and `POST`s to Tidbyt's push API. Best-effort: any
     failure (config missing, render error, HTTP error) is logged at warning and
     swallowed, exactly like `notify_review`.

2. **Tidbyt push API** — `POST https://api.tidbyt.com/v0/devices/{device_id}/push`,
   header `Authorization: Bearer {api_token}`, JSON body
   `{"image": "<base64 webp>", "installationID": "<installation_id>", "background": false}`.
   Re-pushing the same `installationID` replaces crema's single slot in the device's
   app rotation (no pile-up).

3. **Config** (`src/crema/config.py`, `pydantic-settings`) — new optional keys
   auto-loaded from the `CREMA_` prefix, mirroring the Discord pattern:
   - `tidbyt_api_token` ← `CREMA_TIDBYT_API_TOKEN` (default `""`)
   - `tidbyt_device_id` ← `CREMA_TIDBYT_DEVICE_ID` (default `""`)
   - `tidbyt_installation_id` ← `CREMA_TIDBYT_INSTALLATION_ID` (default `"crema"`)

4. **Call site** (`src/crema/review.py`) — enrich `stored` with `profile_name` +
   `bean` from `shots[0]`, then `await tidbyt.push_review(config, stored)` right
   after the existing `notify.notify_review(config, stored)`.

## Rendering approach

**Decision: render in pure Python (Pillow) and POST to Tidbyt's HTTP push API.
No `pixlet`, no `.star`.**

The spec draft recommended shipping a `crema.star` driven by the `pixlet` binary
(the canonical Tidbyt artifact). **That was reversed on a hardware finding:** crema's
target/reference device is a **32-bit armv7 Pi (Pi Zero 2 W)** and `pixlet` ships no
armv7 build (only darwin/linux amd64 + arm64), so the pixlet path would not run on
crema's own hardware. Pillow renders a static WebP on any architecture, adds no Go
toolchain, and matches crema's Python-only, light-footprint ethos.

Trade-off accepted: a static frame can't marquee-scroll a long profile name, so long
names wrap to two lines and ellipsize. Animated-WebP scrolling and/or an optional
community `.star` can come later; neither is needed for v1.

## Error handling

- Missing/partial Tidbyt config → silent no-op (feature off by default).
- Render error (bad/missing fields) → log at warning, no-op; never raises.
- Push HTTP failure/timeout → log at warning, swallow; the review and all other
  channels proceed unaffected (same contract as notify.py).
- Display staleness → the frame self-indicates with a dim corner dot via the
  `stale` flag; crema never blanks the screen.

## Dependency

Add **`Pillow`** to `pyproject.toml` dependencies (with WebP support, which the
standard wheels include). No `pixlet`, no Go toolchain, no new system packages.

## Testing

- Unit (`tests/`, matching the existing offline style — no network, no device):
  - `push_review` no-ops when config unset (patch the HTTP call, assert it is never
    invoked).
  - `render_frame` returns non-empty bytes that start with a WebP signature for a
    representative review; handles a missing/None `score` (defaults to a neutral
    render) and a long `profile` (no exception).
  - `push_review` builds the correct URL + `Authorization` header + JSON body from a
    sample config/review (patch the HTTP client, assert the request args).
  - `push_review` swallows a raised HTTP error without propagating.
- No live device or API token needed in tests.

## Docs

- `deploy/PI_SETUP.md`: set the `CREMA_TIDBYT_*` env vars + how to find the Tidbyt
  device ID / API token (from the Tidbyt app → device settings / `api.tidbyt.com`).
- `README.md`: short "Show your latest shot on a Tidbyt" opt-in section, with the
  same cost/privacy honesty as crema's other integrations (this one is free — no
  Claude/API cost; a local Pillow render + one push to Tidbyt's API).
- `.env.example`: the new `CREMA_TIDBYT_*` keys, commented and off by default.

## Success criteria

With the `CREMA_TIDBYT_*` env vars set on the Pi, finishing a review causes the
Tidbyt to show that shot's score and profile name within one review cycle; with the
vars unset, crema behaves exactly as before and all existing + new tests pass.
