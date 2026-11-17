What this repo does: Crema imports espresso-machine shot history, scores it, asks Claude for advice, and stages profile changes for human approval. It also tracks beans and experiments and optionally shares a dataset. Assumed load: one home installation on a Raspberry Pi, a scheduled reviewer plus occasional overlapping browser/CLI requests, and an internet-facing community collection endpoint.

Audited baseline: `677c59b` on `main`, package version 1.1.0. This audit changes no application code.

## Must fix

1. **Retention cleanup stops ingestion when an experiment references an old review** (`src/crema/db.py:247`, `src/crema/db.py:118`).
   - **What this is:** Every ingest deletes old shots and their reviews after the retention period, normally 30 days.
   - **Problem:** Experiments retain a foreign key to their source review. Deleting that review raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed`; reproduced with a 40-day-old shot and its experiment.
   - **Fix:** Clear the experiment's source-review link before deleting expired reviews, preserving its saved baseline and outcome. Add a cleanup regression test covering both active and closed experiments.
   - **If we skip it:** Normal ingestion and scheduled reviews stop once an experiment's source ages out.

2. **The sharing producer, collector, and importer disagree on the bundle format** (`src/crema/export.py:27`, `share-worker/src/index.ts:62`, `src/crema/dataset.py:13`).
   - **What this is:** Export builds a bundle, sharing adds consent evidence, the Worker stores it, and the importer loads it into SQLite.
   - **Problem:** Export emits schema/terms version 2 and `participant_id`; the Worker requires version 1 and `install_id`. A real generated bundle returns HTTP 400 offline; independently, the strict importer rejects the consent fields that sharing adds.
   - **Fix:** Update the collector and importer to the same explicit versioned contract, including a validated consent envelope. Test one generated, consent-stamped bundle through collection and import, and define handling for existing version-1 submissions.
   - **If we skip it:** Current uploads fail, and stored sharing bundles cannot be imported directly even after the collector is fixed.

3. **A failed review or a separate ingest prevents ordinary review retries** (`src/crema/cli.py:99`, `src/crema/web/app.py:1552`).
   - **What this is:** CLI and web handlers skip paid reviews when no shots were newly imported in that invocation.
   - **Problem:** Once any older review exists, a new shot imported by the timer with auto-review off, by `crema ingest`, or before a failed Claude call is skipped on the next ordinary review. Reproduced with an unreviewed newest shot and a mocked empty ingest result.
   - **Fix:** Gate on whether the newest stored shot has a successful review, rather than whether this invocation inserted it. Reuse that decision in the CLI and web handler and test retry after a failed call.
   - **If we skip it:** The UI says there is nothing to review even though the newest shot has never been reviewed; users must discover a forced or per-shot review workaround.

4. **CLI profile pushes bypass the web approval checks** (`src/crema/cli.py:511`, `src/crema/push.py:21`, `src/crema/web/app.py:1854`).
   - **What this is:** Both CLI and web can push a staged profile, but only the web handler checks cup feedback and acknowledgement of changed stop conditions.
   - **Problem:** `crema push <id>` calls the shared function directly, which checks only draft status. A draft with changed stop conditions reaches the mocked device without acknowledgement; CLI drafting likewise bypasses the web cup-rating gate.
   - **Fix:** Enforce the applicable checks in the shared drafting/push functions, with an explicit acknowledgement argument supplied by the UI or CLI. Preserve the intentional new-bean exception where there is no source shot.
   - **If we skip it:** The same edit has different approval requirements depending on which interface is used, including changes to when a shot stops.

5. **Simultaneous approvals write the same draft to the machine twice** (`src/crema/push.py:26`).
   - **What this is:** Push reads draft status, performs a network write, then changes the status to pushed.
   - **Problem:** Two requests can both read `draft` before either finishes. Reproduced with two SQLite connections and a barrier-controlled device mock: both requests wrote a new profile.
   - **Fix:** Atomically claim the draft in SQLite before the network call and let only the winning request proceed. Treat an ambiguous device timeout as requiring reconciliation, rather than blindly creating another profile on retry.
   - **If we skip it:** A double tap or overlapping CLI/web request creates duplicate profiles and the database retains only one result.

6. **Importing a shorter snapshot duplicates or mixes up shots** (`src/crema/export.py:99`, `src/crema/dataset.py:109`).
   - **What this is:** Every export numbers its currently retained shots starting at 1; import updates matching numbers and keeps unmatched old rows.
   - **Problem:** Export A/B, import it, prune A, then export/import B: B now occupies index 1 while the old B remains at index 2. This was reproduced against temporary source and pool databases.
   - **Fix:** For the existing complete-snapshot format, atomically replace that participant's imported shots and experiments instead of merging unstable indices. If historical accumulation is required, introduce persistent anonymous shot identifiers before merging snapshots.
   - **If we skip it:** Retention changes silently corrupt counts and the links between experiments and shots in the pooled dataset.

7. **Gaggiuino exports are labelled as GaggiMate and lose their detailed telemetry** (`src/crema/export.py:135`, `src/crema/export.py:152`, `src/crema/gaggiuino.py:80`).
   - **What this is:** Both machine adapters feed the same exporter.
   - **Problem:** Export hard-codes `machine="gaggimate"`; its telemetry selection expects `summary` and `diagnostics`, which the Gaggiuino adapter does not produce. Confirmed the wrong machine label with a Gaggiuino configuration and traced the adapter's separate curve/average fields.
   - **Fix:** Use the configured machine and explicitly normalize the supported Gaggiuino metrics into the canonical export. Add an adapter-to-export test with an actual adapter-shaped fixture.
   - **If we skip it:** Gaggiuino contributions are misclassified and important measurements disappear.

8. **Gaggiuino shots never join active experiments** (`src/crema/gaggiuino.py:169`, `src/crema/ingest.py:77`).
   - **What this is:** New matching-bean shots should automatically become experiment follow-ups.
   - **Problem:** The GaggiMate path calls `assign_shot_to_active_experiment`; the Gaggiuino path stores a shot and returns without that call. A whole-tree caller search confirms there is no later assignment path for this adapter.
   - **Fix:** Call the existing assignment helper after successful Gaggiuino insertion and cover it with a mocked ingest test.
   - **If we skip it:** Gaggiuino experiments stay waiting for follow-ups and cannot reach a supported keep/revert conclusion.

## Should fix

9. **An unrated experiment baseline cannot be repaired by rating the shot later** (`src/crema/db.py:610`, `src/crema/db.py:663`).
   - **What this is:** Starting an experiment copies the baseline cup rating; keeping or reverting later requires that copied value.
   - **Problem:** Start endpoints accept an unrated baseline, and the experiment does not store a baseline shot ID for the manual path. Rating the original shot later leaves `baseline_cup` null; reproduced with rated baseline and follow-up shots followed by a rejected conclusion.
   - **Fix:** Reject unrated baselines before closing any existing run or starting a new one. If retrospective baseline edits are intended, persist the source shot ID and define how edits update the baseline.
   - **If we skip it:** Users can create a run that cannot be concluded normally even after following the instruction to rate its cups.

10. **Jinja2 is installed but never used** (`pyproject.toml:22`).
    - **What this is:** Jinja2 is a template engine, while Crema renders its HTML directly in Python.
    - **Problem:** A whole-tree search, including hidden configuration and tests but excluding installed packages and Git internals, finds only the dependency declaration and lockfile references.
    - **Fix:** Remove the direct dependency and refresh the lockfile when implementing release fixes.
    - **If we skip it:** Every installation carries an unnecessary package and its maintenance burden.

## Verification and coverage

- Installed the committed lockfile with Python 3.13.14 using `uv sync --frozen`.
- Existing test suite: **62 passed**, with one Starlette TestClient deprecation warning.
- Additional temporary offline checks reproduced findings 1–6, the machine-label portion of 7, and 9. All device writes and review calls in those checks were mocked; no live machine or paid AI service was called.
- Ran the actual Worker source locally through Node's TypeScript support with a generated version-2 bundle and a fake R2 bucket; it returned HTTP 400 before storage.
- Read the application data/CLI paths, auth and mutation handlers, profile draft/push pipeline, exporters/importer, adapters, scoring and starting-point code, notifications, deployment configuration, CI, and related test coverage. Inspected the used vendored HTTP/WebSocket clients and transformer shapes.
- Inspected the five commits after `v1.1.0`; the package still reports 1.1.0. New changes need a distinct release record.

Verdict: Fix retention, the sharing contract, review retry detection, and shared profile-approval handling before shipping the next release. The green suite does not exercise these boundaries.

Lean: -1 dependency declaration line, -1 direct dependency possible, plus generated lockfile cleanup.

Not checked: live GaggiMate/Gaggiuino hardware, real Claude output or billing, Tidbyt/Discord delivery, the deployed Worker/R2 configuration, browser visual/accessibility behavior, sustained load, dependency vulnerability advisories, or a line-by-line audit of the unused vendored MCP server/storage modules and the full binary parser/physics algorithms. No audit fixes were applied.


## Resolution record — 2026-11-17

All ten numbered findings have local fixes on `release/1.2.0`. The findings above
remain the historical record of baseline `677c59b`.

- 1: Null expired source-review links before retention deletes reviews; preserve experiments.
- 2: Collector accepts version-2 envelopes; importer validates consent fields; legacy version-1 objects remain untouched and are rejected for new canonical imports/uploads.
- 3: Both review entry points check for a successful review of the newest stored shot.
- 4: Shared draft/push code enforces source-cup ratings and explicit stop-change acknowledgement.
- 5: Conditional SQLite claims allow one push; ambiguous results remain blocked. Refinement and discard cannot overwrite claimed pushes.
- 6: Import replaces a participant's shot/experiment snapshot in a transaction and rolls back on failure.
- 7: Export uses the configured machine and includes explicitly selected normalized Gaggiuino metrics/curves.
- 8: Gaggiuino ingest assigns new shots through the existing experiment helper.
- 9: Reject unrated baselines before changing the current run; existing unrated runs can be replaced by a new rated run.
- 10: Removed Jinja2; lockfile also drops the now-unused MarkupSafe package.

These are local code fixes, not a production deployment or live-machine certification.

Verification after fixes: **75 Python tests and 3 Worker contract tests pass**;
TypeScript checking and `git diff --check` pass. Worker tests run through the same
`uv run --project .. npm test` command used in CI, and Wrangler's deployment dry-run
successfully bundles the Worker without deploying it. The dependency refresh also
removed four reported npm advisories; installation reported zero remaining.
The existing Starlette TestClient deprecation warning remains.
