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
- **Deterministic execution scoring.** The 1–10 chart score now comes from
  repeatable telemetry penalties (channeling, temperature stability, profile
  adherence, resistance erosion), rather than varying model judgment. Claude's
  assessment is retained for diagnosis; a separate 1–5 **cup rating** records
  what the coffee actually tasted like.
- **Recipe targets per bean.** Save a preferred dose, yield, profile, and cup
  style for each bean. Yield and profile penalties apply only when you have
  explicitly set those targets, so crema never forces a generic espresso recipe.
- **Dial-in experiments and outcome tracking.** Record one deliberate change,
  then crema automatically groups new matching-bean shots until you finish the
  experiment and reports execution/cup-rating movement from the baseline.
- **Shot comparison (`/compare`).** Select any two shots for a compact visual
  comparison of execution, time, and yield, plus side-by-side diagnostics.
- **Tidbyt display (opt-in).** Push the latest reviewed shot's score and profile
  to a Tidbyt display from the same Pi; it is a local render plus one API push.

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
- Recipe-target profile selection no longer fails when rendering the report.

[1.1.0]: https://github.com/waevans10/crema/releases/tag/v1.1.0
