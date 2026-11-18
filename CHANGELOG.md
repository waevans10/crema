# Changelog

All notable changes to crema are documented here. This project adheres to
[Semantic Versioning](https://semver.org).

## [1.3.0] — 2026-11-17

### Grind & dose memory

### Added

- Grind setting and dose recorded per shot beside the cup rating, prefilled from the
  latest earlier shot on the same bean (or the bean's target dose).
- "Your best on <bean>" card on Now: the highest-rated shot as grind · dose in →
  yield out, with one tap to save it as the bean's recipe.
- Reviews include the recorded grind and dose for each shot.
- Shot notebook summaries show recorded grind and dose.

### Fixed

- A failed review now waits 3 hours before retrying the same shot instead of
  re-calling Claude on every 15-minute timer run. Manual reviews (web button,
  `--force`) still retry immediately.
- Pushes with an unknown outcome can be discarded (web and CLI) after checking the machine.
- Community imports lowercase participant IDs to match the collector's storage keys.
- `crema discard` on a non-draft prints a clear error instead of a traceback.

### Upgrade notes

- Back up your SQLite database, run `uv sync --frozen`, and restart the Crema
  service. Two nullable shot columns are added automatically.

## [1.2.0] — 2026-11-17

### Dial-in workflow and reliability

### Added

- Self-guided dial-in guidance without requiring a paid AI review.
- Structured experiment outcomes with keep, revert, and continue decisions.
- Canonical version-2 community dataset exports and a SQLite importer.

### Fixed

- Retention preserves experiment baselines while removing expired source reviews.
- Stored, unreviewed shots are retried after ingest-only runs or review failures.
- CLI and web share cup-rating and stop-change approval checks.
- Atomic draft claims prevent duplicate pushes; uncertain device writes are not retried.
- Refinement/discard cannot overwrite an in-progress push.
- Version-2 sharing envelopes work through collection and import; imports atomically
  replace participant snapshots to remove stale indices.
- Gaggiuino shots join experiments and retain their machine identity and normalized
  measurements in exports.
- Unrated baselines are rejected before an existing experiment is closed.

### Maintenance

- Removed unused Jinja2 and its now-unneeded MarkupSafe dependency.
- Added offline audit regressions and Worker contract/type checks to CI.
- Updated Wrangler and matching Cloudflare types to clear reported tooling advisories.

### Upgrade notes

- Back up your SQLite database before upgrading, then run `uv sync --frozen`
  and restart the Crema service.
- Review-based profile edits require a source cup rating. CLI pushes with changed
  stop conditions also require `--acknowledge-stops` after reviewing the changes.
- Deploy the updated sharing Worker before sending version-2 bundles. Older
  version-1 submissions are rejected; existing stored objects are left untouched.
- Dataset imports replace a participant's current snapshot; keep raw submissions
  for historical analysis and import snapshots in collection order.
- An uncertain device write is not automatically retried. Check the machine for
  the saved profile before creating another draft.
- Worker development/CI requires Node 22.18 or newer (CI uses Node 24).

### Validation

75 offline Python tests, 3 Worker contract tests, TypeScript checking, and a
Worker deployment dry-run pass. Live espresso hardware, paid AI calls, and
production Worker deployment were not exercised for this release.

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

[1.2.0]: https://github.com/waevans10/crema/releases/tag/v1.2.0
