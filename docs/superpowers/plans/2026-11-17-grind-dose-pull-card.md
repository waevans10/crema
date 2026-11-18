# Grind & Dose Per Shot + Pull-This Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record grind setting and dose on every shot, feed them to Claude, and show the best-rated shot on the active bean as a "pull this" card on Now.

**Architecture:** Two nullable `shots` columns via the existing additive migration in `db.py`; three small db helpers; the existing `/shots/{id}/tasting-notes` route saves the new fields; the existing server-rendered forms in `web/app.py` gain two inputs; one new card and one new POST route.

**Tech Stack:** Python 3.13, aiosqlite, FastAPI with hand-written HTML strings (no templates, no JS), pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-grind-dose-pull-card-design.md`

## Global Constraints

- `grind_setting TEXT`, stripped, max 20 chars, empty → NULL.
- `dose_g REAL`, accepted 5–30 inclusive; a comma decimal (`18,5`) is accepted as `18.5`; anything else non-empty is rejected with an error and nothing on the shot is saved.
- Card only when the active bean has a shot rated 4 or 5; ties → most recent (`COALESCE(captured_at, created_at)`, then rowid).
- Yield on the card = the shot's `transformed["final_weight_g"]`; missing parts are omitted, never shown blank.
- "Save as this bean's recipe" changes only `target_dose_g` / `target_yield_g`; keeps `target_profile_id` and `cup_style`.
- Not added to `export.py` (community bundle stays unchanged).
- All user text rendered with `html.escape`. No JavaScript. Works at phone width, light and dark (reuse existing `card`, `btn`, `muted`, `row` classes and CSS variables).

## Review Focus

1. Editing notes on an **old** shot from the notebook must not stamp it with today's grind — prefill uses the shot's own values first, else the latest **earlier** shot on that bean. (Test in Task 2.)
2. Grind text containing `<script>` or quotes renders escaped in the form value and the card. (Test in Task 3.)
3. A shot with no `bean_id` gets no prefill and never produces a card; no crash. (Tests in Tasks 2 and 3.)
4. Dose `"abc"`, `"0"`, `"31"` → error redirect, shot unchanged (rating/notes also not saved). (Test in Task 1.)
5. Best shot missing dose, grind or weight → card shows only what exists. (Test in Task 3.)

---

### Task 1: Store, save and send grind & dose

**Files:**
- Modify: `src/crema/db.py` (migration ~L166-180, `_shot_row` L327, `get_shot` L339, `recent_shots` L312 SELECT lists)
- Modify: `src/crema/web/app.py` `save_tasting_notes` (~L1794)
- Modify: `src/crema/prompts.py` `build_user_message` (~L410, after the cup-rating line)
- Test: `tests/test_grind_dose.py` (new)

**Interfaces:**
- Produces: shot dicts gain `"grind_setting": Optional[str]`, `"dose_g": Optional[float]`.
- Produces: `async def set_shot_grind_dose(db, shot_id: str, grind: Optional[str], dose_g: Optional[float]) -> bool` in `db.py` (commits; False if no row).
- Produces: `def parse_dose(raw: str) -> Optional[float]` in `web/app.py` — `""` → None; valid → float; invalid → `ValueError("Dose must be a number of grams between 5 and 30.")`.
- Route form gains `grind: str = Form("")`, `dose: str = Form("")`.

- [ ] **Step 1: Write failing tests** in `tests/test_grind_dose.py` (use the async `conn` fixture pattern from `tests/test_audit_regressions.py`):
  - `test_grind_dose_round_trip`: upsert shot `'1'`; `set_shot_grind_dose(conn,'1','12',18.0)` is True; `get_shot` and `recent_shots` both return `grind_setting=='12'`, `dose_g==18.0`; `set_shot_grind_dose(conn,'999','1',18)` is False.
  - `test_parse_dose`: `parse_dose('')` is None; `parse_dose('18,5')==18.5`; `parse_dose('5')==5`; `parse_dose('30')==30`; each of `'abc'`, `'0'`, `'31'`, `'4.9'` raises `ValueError`.
  - `test_route_rejects_bad_dose_and_saves_nothing`: point `web._cfg` at the tmp db (as in `test_web_reviews_already_ingested_shot_and_retries_failure`); call `await web.save_tasting_notes('1', notes='x', coffee='', cup_rating='4', grind='12', dose='31')`; response location contains `error=`; shot has `cup_rating is None`, `tasting_notes is None`, `grind_setting is None`.
  - `test_route_saves_grind_dose`: same call with `dose='18'` → shot has `grind_setting=='12'`, `dose_g==18.0`, `cup_rating==4`. Grind `'x'*30` is stored as 20 chars.
  - `test_prompt_includes_grind_dose`: `build_user_message([shot])` where shot has `grind_setting='12'`, `dose_g=18.0` contains `"GRIND: 12"` and `"DOSE: 18.0 g"`; a shot without them contains neither.

- [ ] **Step 2: Run** `uv run pytest tests/test_grind_dose.py -q` — expect failures (missing function/columns).

- [ ] **Step 3: Implement.** Migration: `ALTER TABLE shots ADD COLUMN grind_setting TEXT` and `... dose_g REAL` guarded by `shot_cols` like the others. Add both columns to the three SELECTs and `_shot_row`. Route: parse dose first (on `ValueError` → `_redirect_error`-style `/?error=` redirect before any write), then the existing writes plus `set_shot_grind_dose(conn, shot_id, grind.strip()[:20] or None, dose)`. Prompt line: `GRIND: {grind_setting} · DOSE: {dose_g} g` (emit each part only when present).

- [ ] **Step 4: Run** `uv run pytest -q` — all pass.

- [ ] **Step 5: Commit** `feat: record grind and dose per shot and send them to reviews`.

---

### Task 2: Grind & dose inputs, prefilled

**Files:**
- Modify: `src/crema/db.py` (new helper next to `recent_shots`)
- Modify: `src/crema/web/app.py` `_workflow_prompt` (~L1086) and `_render_shots` notebook form (~L795)
- Test: `tests/test_grind_dose.py`

**Interfaces:**
- Consumes: Task 1 columns and route fields `grind`, `dose`.
- Produces: `async def grind_dose_prefill(db, shot: dict) -> tuple[Optional[str], Optional[float]]` in `db.py`: the shot's own values where set; else from the most recent shot with the same `bean_id` captured **before** it that has a value (each field independently); dose finally falls back to the bean's `target_dose_g`; `bean_id` None → the shot's own values only.
- Produces: `def _grind_dose_inputs(grind: Optional[str], dose: Optional[float]) -> str` in `web/app.py` — two labelled inputs, `name='grind'` (text, `maxlength=20`, `placeholder='e.g. 12'`) and `name='dose'` (`inputmode='decimal'`, `placeholder='18'`), values escaped.

- [ ] **Step 1: Write failing tests:**
  - `test_prefill_uses_earlier_shot_on_same_bean`: bean B (via `db.add_bean`/existing bean helper) with `target_dose_g=18`; shot `'1'` (t=100, bean B, grind `'12'`, dose 18.5); shot `'2'` (t=200, bean B, none) → `('12', 18.5)`.
  - `test_prefill_never_uses_later_shot`: shot `'3'` (t=300, bean B, grind `'9'`); prefill for shot `'2'` still `('12', 18.5)`; prefill for `'1'` is its own `('12', 18.5)`.
  - `test_prefill_falls_back_to_bean_dose`: bean with `target_dose_g=17`, single shot without values → `(None, 17.0)`.
  - `test_prefill_without_bean`: shot with `bean_id=None` → `(None, None)`.
  - `test_forms_render_prefilled_inputs`: `_workflow_prompt(shot2, bean, None)` (shot unrated) contains `name='grind'` with `value='12'`; `_render_shots([shot], {})` output contains `name='dose'`. (Pass the prefill in however the render functions take it; the test asserts the HTML.)

- [ ] **Step 2: Run** the new tests — expect failures.

- [ ] **Step 3: Implement.** `_render_shots` and `_workflow_prompt` are sync, so compute prefill per shot in the async page handler and pass a `dict[shot_id, tuple]` in (default empty). Put `_grind_dose_inputs` beside the rating select in both forms; in the baseline card keep the `row` layout so it wraps on phones.

- [ ] **Step 4: Run** `uv run pytest -q` — all pass.

- [ ] **Step 5: Commit** `feat: prefilled grind and dose inputs on rating forms`.

---

### Task 3: Pull-this card and save-as-recipe

**Files:**
- Modify: `src/crema/db.py` (new helper)
- Modify: `src/crema/web/app.py` (new `_pull_card`, rendered on Now directly under `_aging_banner(active_bean)`; new route next to `save_recipe` ~L1625)
- Test: `tests/test_grind_dose.py`

**Interfaces:**
- Consumes: Task 1 shot fields.
- Produces: `async def best_shot_for_bean(db, bean_id: int) -> Optional[dict]` — shot dict (same shape as `get_shot`) with `cup_rating >= 4`, highest rating, ties → most recent; None if none.
- Produces: `def _pull_card(bean: dict, shot: Optional[dict]) -> str` — `""` when shot is None.
- Produces: `POST /beans/{bean_id}/recipe-from-shot` form field `shot_id: str` → copies `dose_g` and `final_weight_g` into the bean via `db.set_bean_recipe(conn, bean_id, dose, yield, bean['target_profile_id'], bean['cup_style'])`; 303 to `/?note=...` on success, `/?error=...` if bean/shot missing or the shot's `bean_id` differs.

- [ ] **Step 1: Write failing tests:**
  - `test_best_shot_picks_highest_then_newest`: bean B shots rated 4 (t=100), 5 (t=200), 5 (t=300), 3 (t=400) → id of t=300; bean with only ≤3 ratings → None.
  - `test_pull_card_renders_parts_and_escapes`: shot grind `'<b>12'`, dose 18, weight 36, rating 5 → HTML contains `&lt;b&gt;12`, `18 g in`, `36 g out`, `★★★★★`, and `recipe-from-shot`; shot with no grind/dose/weight → no `Grind`, no ` g in`, still the stars; `_pull_card(bean, None) == ""`.
  - `test_recipe_from_shot_keeps_profile_and_style`: bean with `target_profile_id='p1'`, `cup_style='sweet'`; route with the best shot → bean has `target_dose_g==18`, `target_yield_g==36`, profile `'p1'`, style `'sweet'`; a shot from another bean → `error=` and bean unchanged.

- [ ] **Step 2: Run** the new tests — expect failures.

- [ ] **Step 3: Implement.** Card copy: heading `Your best on {bean name}`, line `Grind {g} · {dose} g in → {yield} g out · ★…`, muted `pulled {_fmt_shot_time(shot)}`, small ghost button `Save as this bean's recipe`. Hide the button when the bean's targets already equal the shot's dose/yield. Format numbers with the existing `_fmt_qty` style (no trailing `.0`).

- [ ] **Step 4: Design pass.** Load `design:ux-copy` for the card and input copy, and `design:accessibility-review` for the inputs (labels tied to inputs, tap targets, contrast in dark mode). Apply only fixes inside this card and these inputs.

- [ ] **Step 5: Run** `uv run pytest -q` — all pass. Then `uv run crema serve` against a scratch DB with a seeded bean and rated shots and check the Now page in a browser at phone width, light and dark.

- [ ] **Step 6: Commit** `feat: pull-this card with save-as-recipe on Now`; update `CHANGELOG.md` Unreleased and the README "What it does" list in the same commit.
