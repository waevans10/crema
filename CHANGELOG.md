# Changelog

All notable changes to crema are documented here. This project adheres to
[Semantic Versioning](https://semver.org).

## [1.1.0] — 2026-07-05

### the cold-start release

crema could always improve a shot you'd already pulled. Now it can give you a
**first shot for a bean you've never touched** — and show you your progress over
time.

**Added**

- **Start a new bean → a starting-point profile.** Add a bag (name, roast level,
  process, roast date) and crema generates a starting grind, dose, target
  yield/ratio, and a complete, push-ready profile. It leans on your **most
  similar past shots** — matched by roast level and origin from your own shot
  history — and falls back to roast-level first principles when nothing similar
  exists. Available in the web UI ("Start a new bean"), and as `crema bean` /
  `crema start` on the CLI. The result is **staged for one-tap approval** like
  any other edit — nothing is pushed to the machine automatically.
- **Structured bean library.** A restricted-vocabulary `beans` table (roast level
  is validated in both Python and the DB) keeps bean data consistent so
  similarity matching is reliable. New shots are linked to the active bean.
- **Trends dashboard (`/trends`).** Shot score over time as server-rendered SVG —
  a 7-shot rolling average, poor/okay/good bands, and dashed markers where your
  beans changed — plus a scannable recent-shots table. No JavaScript, no charting
  library; it stays light on a Pi.
- **Bean-aging warning.** Beans past their prime (default 30 days off roast,
  `CREMA_BEAN_MAX_AGE_DAYS`) show a heads-up in the web UI.
- **Token usage is now persisted** with each review, for after-the-fact cost
  audits (in keeping with crema's cost-transparency promise).

**Changed**

- **Cleaner report header.** The top control stack is consolidated; grinder and
  coffee settings move into a collapsible Settings panel; a nav link points at
  Trends. Same lightweight, zero-framework design system.

**Fixed**

- New shots are now linked to the active structured bean at ingest (previously
  only the freetext coffee string was stamped).
- `prune_old` uses parameterized SQL instead of string interpolation.
- `CREMA_MACHINE` is validated (`gaggimate` | `gaggiuino`) so a typo fails loudly
  instead of silently defaulting.
- The compact past-review summary interleaved into review context is now capped,
  so an edit with many profile changes can't bloat the window.
- Best-effort profile-cache failures during ingest are logged at debug level
  instead of being swallowed silently.

[1.1.0]: https://github.com/waevans10/crema/releases/tag/v1.1.0
