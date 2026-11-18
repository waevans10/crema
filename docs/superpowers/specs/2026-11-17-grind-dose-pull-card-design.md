# Grind & dose per shot, and the "pull this" card

## Why

crema records telemetry, taste notes and a cup rating per shot, but not the grinder
setting or the dose. Grind is the variable changed most while dialing in, so Claude
advises "go finer" without knowing the current setting, nothing remembers where a
bean landed last time, and grind/dose experiments never record actual values.

## Goals

- Record grind setting and dose on every shot, with near-zero typing.
- Show, at the top of **Now**, the best-rated shot on the active bean as a recipe to
  repeat: grind · dose in → yield out · rating.
- Give Claude the grind and dose for each shot in the review window.

## Non-goals

- No grinder brand/model profiles or unit conversions.
- No new pages, charts, notifications or JavaScript.
- Not added to the community share bundle (it would change the v2 data contract and
  terms) — revisit separately.

## Anti-goals

- A stale value silently recorded as truth. Prefilled boxes must sit visibly next to
  the rating so the barista sees what is being saved.

## Approaches

- **A. Grind only, per shot** — least typing, but dose on the card would be the bean
  target, not what was used.
- **B. Grind + dose per shot, prefilled (chosen)** — card and Claude see actual values;
  dose experiments get real numbers. Two columns instead of one.
- **C. Grind on the bean recipe only** — dropped: no history, so nothing to learn from.

## Design

**Data.** Two nullable columns on `shots`, added through the existing additive
migration in `db.py`:
- `grind_setting TEXT` — short free text (≤ 20 chars). *My call:* text, not a number,
  because grinders use numbers, clicks and "1 turn + 4"-style marks.
- `dose_g REAL` — grams, accepted 5–30.

**Entry.** The two existing rating forms — the "taste this baseline" card and the shot
notebook form — gain a Grind box and a Dose (g) box beside the rating. Both post to the
existing `/shots/{id}/tasting-notes` route, which validates and saves them. Prefill
order: the most recent shot on the same bean that has a value → the bean's
`target_dose_g` (dose only) → empty.

**Card.** At the top of Now, when the active bean has a shot rated 4 or 5: the
highest-rated such shot (ties → most recent) shown as
`Grind 12 · 18 g in → 36 g out · ★★★★★`, with when it was pulled. Yield is the
measured `final_weight_g`. Missing parts are left out, not shown as blanks. A
"Save as this bean's recipe" button copies its dose and yield into the bean's
`target_dose_g` / `target_yield_g` (keeping profile and cup style), via a small POST
route.

**Claude.** `build_user_message` adds `grind: <x>, dose: <y> g` to each shot line when
present.

## Left to the builder

Exact markup and copy (follow the page's existing card styles and work at phone width,
light and dark); the card's route name; tests — one for save/prefill/validation, one
for best-shot selection.
