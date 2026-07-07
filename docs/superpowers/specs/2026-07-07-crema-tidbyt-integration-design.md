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
| `profile_name` | profile label of the reviewed shot (the human name shown in the report / used by notify.py; the `[AI]` profile name when the shot ran an AI profile) |
| `bean` (optional, for context line) | current bean / `coffee` string for that shot |
| `reviewed_at` | `review["created_at"]` |

**Open item for planning:** pin the exact source of `profile_name` for both
GaggiMate and Gaggiuino shots (Gaggiuino exposes `profile_name`; confirm the
GaggiMate shot's profile label field). Reuse whatever notify.py / the web report
already display so all three surfaces agree.

## Components

New module `src/crema/tidbyt.py`, plus a Starlark app file and config keys.

1. **`crema.star`** (`apps/tidbyt/crema.star`) — the Tidbyt app. Renders a 64×32
   frame: large score number (color-coded red<4 / amber<7 / green≥7, matching
   notify.py's `_score_color`), the profile name scrolling beneath, a small "espresso"
   glyph, and a dim "stale" dot if the data is older than a configurable age.
   Takes `score`, `profile`, `bean`, `age` as `pixlet render` config params — no
   secrets and no network calls inside the app.

2. **`src/crema/tidbyt.py`** — `push_review(config, review)`:
   - No-op unless Tidbyt config is set (`CREMA_TIDBYT_API_TOKEN` +
     `CREMA_TIDBYT_DEVICE_ID`).
   - Builds config params from the review, shells out to `pixlet render crema.star
     <params> -o <tmp.webp>`, then `pixlet push --api-token … --installation-id crema
     <device-id> <tmp.webp>`.
   - Best-effort: any failure (missing pixlet, render error, HTTP error) is logged
     at warning and swallowed.

3. **Config** (`src/crema/config.py`) — new optional keys, mirroring the Discord
   pattern:
   - `tidbyt_api_token` ← `CREMA_TIDBYT_API_TOKEN`
   - `tidbyt_device_id` ← `CREMA_TIDBYT_DEVICE_ID`
   - `tidbyt_installation_id` ← `CREMA_TIDBYT_INSTALLATION_ID` (default `crema`)
   - `pixlet_bin` ← `CREMA_PIXLET_BIN` (default `pixlet`)

4. **Call site** — wherever `notify_review` is currently awaited after a review is
   stored, add the `push_review` call alongside it.

## Rendering approach

**Recommended: ship `crema.star` and drive it with the `pixlet` binary.** This is
the canonical Tidbyt artifact — the community expects a `.star` app, and pixlet
gives proper scrolling/fonts for the profile name. Cost: a per-arch Go binary
(`pixlet`) must be present on the Pi; `deploy/PI_SETUP.md` gains a one-line install
note, and the feature no-ops cleanly if `pixlet` is absent.

**Alternative considered (Pillow → Tidbyt push API):** render the frame in pure
Python and POST base64 WebP to `api.tidbyt.com/v0/devices/{id}/push`. Zero binary
dep, but no reusable `.star` for the community and hand-rolled text layout. Rejected
as the default because the community deliverable *is* a Starlark app. **This is the
one decision to confirm at spec review.**

## Error handling

- Missing/partial Tidbyt config → silent no-op (feature off by default).
- `pixlet` not installed → log once at warning, no-op.
- Render/push failure → log at warning, swallow; the review and all other channels
  proceed unaffected (same contract as notify.py).
- Display staleness → the app self-indicates with a dim dot using the `age` param;
  crema never blanks the screen.

## Testing

- Unit: `push_review` no-ops when config unset; builds correct pixlet arg vectors
  from a sample review (monkeypatch the subprocess, assert argv); clamps score;
  handles missing `score`/`profile_name`; swallows a raised subprocess error.
- `crema.star`: `pixlet render` succeeds locally with representative params
  (checked in CI only if pixlet available; otherwise a smoke note in PI_SETUP).
- No live device or API token needed in tests.

## Docs

- `deploy/PI_SETUP.md`: install pixlet + set the two env vars + how to find the
  Tidbyt device ID / API token.
- `README.md`: short "Show your latest shot on a Tidbyt" opt-in section, with the
  same cost/privacy honesty as crema's other integrations (this one is free — no
  API cost; local render + a push to Tidbyt's API).
- `.env.example`: the new `CREMA_TIDBYT_*` keys, commented and off by default.

## Success criteria

With the env vars set on the Pi and pixlet installed, finishing a review causes the
Tidbyt to show that shot's score and profile name within one review cycle; with the
vars unset, crema behaves exactly as before and all tests pass.
