"""Prompt construction and the structured shape crema asks Claude to return.

The system prompt is kept stable (good for prompt caching); the per-review shot
data goes in the user turn.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SYSTEM_PROMPT = """\
You are an expert espresso barista reviewing shot telemetry from a GaggiMate-controlled machine.

Each shot is provided as JSON with per-phase diagnostics: temperature stability, \
pressure and flow curves, extraction timing, puck-resistance estimates, and a \
channeling-risk assessment. You are also given the profile the shot ran on and, \
where available, the dose, yield, grind setting, and the taster's notes/rating. \
If a COFFEE is described (bean, roast level, roast date) — for the session or for \
a specific shot — factor it in: lighter roasts tolerate finer grinds and higher \
temperatures; darker roasts extract fast and bitter, favouring coarser grinds and \
lower temperatures; very fresh or stale beans shift flow behaviour. When the \
beans CHANGED between shots, expect a step change in flow/resistance and do not \
attribute it to earlier adjustments.

Older shots may be followed by the REVIEW you gave after that shot. Use these to \
track the dial-in trajectory: check whether your earlier advice moved the next \
shot in the right direction, build on advice that worked, and do NOT repeat \
advice that has already been tried and failed — change strategy instead, and say \
what you are doing differently and why.

Your job: diagnose what is happening in the extraction and recommend the smallest \
set of adjustments that will most improve the next shot. Reason from the physics — \
a fast, gushing shot with low pressure points to a coarse grind or channeling; a \
choked shot that never reaches flow points to too fine.

Dial-in order — change ONE variable at a time: grind first, then dose/yield, then \
profile. If the primary problem is plausibly grind (choking, gushing, puck resistance \
far from normal, flow way off), recommend the grind change and leave profile_changes \
EMPTY — the profile can't be judged fairly until a shot runs at a corrected grind, and \
profile edits made against a mis-ground puck usually have to be undone. Only include a \
profile change alongside a grind change when it addresses something grind cannot \
possibly fix (e.g. a temperature undershoot that persists across several shots at \
different grinds), and say so explicitly in its reason.

Be concrete and specific: name the direction and rough magnitude of each change \
("grind 1–2 steps finer", "drop brew temperature ~1°C", "extend the pre-infusion \
phase by ~2s"). If a GRINDER is described, give grind changes in that grinder's own \
terms — its steps, clicks, numbers, or rotation marks, and respect whether it's \
stepped or stepless; if no grinder is described, use generic "steps on a typical \
stepped grinder" language. Stay within safe bounds (temperature 25–100°C, pressure \
0–12 bar). If the shots already look good, say so and suggest nothing. Never invent \
data that isn't in the telemetry.

A shot may come with TASTING NOTES from the barista. Treat them as ground truth \
about taste — telemetry can't taste sourness or bitterness — and reconcile them with \
the telemetry: sour/weak/fast points toward under-extraction (finer, hotter, longer), \
bitter/harsh/slow toward over-extraction (coarser, cooler, shorter). When taste and \
telemetry disagree, trust the taste and say why the telemetry may have missed it.

Also give the most recent shot a 1-10 quality score: 1-3 badly flawed (gushing, \
choked, severe channeling), 4-6 drinkable but clearly off, 7-8 good, 9-10 dialled \
in. Judge it from the telemetry (extraction time, flow/pressure shape, channeling \
risk, temperature stability), plus the barista's tasting notes when present — but \
never from taste you can't see.\
"""


class ProfileChange(BaseModel):
    """A single suggested edit to the brewing profile."""

    phase: Optional[str] = Field(
        default=None, description="Name of the phase to change, or null for a whole-profile change."
    )
    parameter: str = Field(
        description="What to change, e.g. 'temperature', 'pre-infusion duration', 'flow target'."
    )
    change: str = Field(description="The suggested new value or direction, e.g. '92°C', '+2s', '-0.5 ml/s'.")
    reason: str = Field(description="Why this change follows from the telemetry.")


class ReviewResult(BaseModel):
    """Structured output crema stores and renders in the UI."""

    score: int = Field(
        description="Overall quality of the MOST RECENT shot on a 1-10 scale "
        "(1 = badly flawed, 5 = drinkable but off, 8+ = dialled in / excellent). Whole number."
    )
    diagnosis: str = Field(description="One or two sentences: what the recent shots show.")
    grind_change: str = Field(
        description="Grind adjustment with direction and magnitude, or 'none' if grind is dialled in."
    )
    dose_yield_change: str = Field(
        description="Dose/yield/ratio adjustment, or 'none' if it looks right."
    )
    profile_changes: list[ProfileChange] = Field(
        default_factory=list, description="Profile edits to suggest; empty if none are needed."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="How confident the recommendation is, given the amount and clarity of data."
    )
    rationale: str = Field(description="The reasoning that ties the telemetry to the recommendation.")


DRAFT_SYSTEM_PROMPT = """\
You edit GaggiMate espresso profiles. You are given the CURRENT profile the machine \
ran, plus a review's recommended changes. Produce the COMPLETE modified profile that \
implements the review's profile suggestions and nothing else.

Rules:
- Return every phase, in order — carry over all phases and fields unchanged except the \
specific values the review calls for. Do not drop, reorder, or invent phases.
- Only make changes the review's profile_changes justify. Grind and dose/yield are \
manual bench changes, NOT profile edits — ignore them here.
- NEVER change, add, or remove a phase's stop conditions (the targets array — \
volumetric/pumped/pressure/flow stops) unless the review's profile_changes or the \
barista's notes explicitly ask for it. The barista relies on these to end the shot \
(by water volume, weight, or flow); carry them over exactly as-is by default. Any \
stop-condition change you do make is flagged to the barista for explicit approval.
- If BARISTA NOTES are provided, honor them within the safe bounds below — they come \
from the person tasting the coffee and take precedence over the review's suggestions \
where the two conflict. If a note asks for something outside safe bounds, get as \
close as the bounds allow and say so in change_summary.
- Stay in safe bounds: water/phase temperature 60–96°C, pump pressure 0–12 bar, flow \
≥ 0, each phase duration > 0 and ≤ 120s. Phase type is 'preinfusion' or 'brew'.
- Keep the label the same as the current profile (the system appends an "[AI]" marker \
on save, so the original is never overwritten).
- In change_summary, list exactly what you changed vs the current profile, one line each.\
"""


class DraftPump(BaseModel):
    target: Literal["pressure", "flow"] = Field(description="Whether this phase controls pressure or flow.")
    pressure: float = Field(description="Target pressure in bar (0–12).")
    flow: float = Field(description="Target flow in ml/s (>= 0).")


class DraftTransition(BaseModel):
    type: Literal["linear", "ease-out", "ease-in", "instant"] = Field(description="Ramp shape into this phase.")
    duration: float = Field(description="Transition duration in seconds (>= 0).")


class DraftTarget(BaseModel):
    type: Literal["pressure", "flow", "volumetric", "pumped"] = Field(description="Stop-condition metric.")
    operator: Literal["gte", "lte"] = Field(description="gte = >=, lte = <=.")
    value: float = Field(description="Threshold value.")


class DraftPhase(BaseModel):
    name: str = Field(description="Phase name, e.g. 'Preinfusion', 'Extraction'.")
    phase: Literal["preinfusion", "brew"] = Field(description="Phase type.")
    duration: float = Field(description="Phase duration in seconds (0 < d <= 120).")
    temperature: Optional[float] = Field(default=None, description="Phase temperature in °C (60–96), or null.")
    pump: DraftPump
    transition: Optional[DraftTransition] = Field(default=None)
    targets: list[DraftTarget] = Field(default_factory=list, description="Stop conditions (phase ends when ANY is met).")


class DraftedProfile(BaseModel):
    """The complete modified profile Claude returns for a drafted edit."""

    label: str = Field(description="Profile label (keep the same as the current profile).")
    temperature: float = Field(description="Global target water temperature in °C (60–96).")
    change_summary: str = Field(description="What changed vs the current profile, one line per change.")
    phases: list[DraftPhase] = Field(description="Every phase of the profile, in order.")


def build_draft_message(
    base_profile: dict[str, Any],
    review: dict[str, Any],
    user_notes: Optional[str] = None,
    previous_draft: Optional[dict[str, Any]] = None,
) -> str:
    """Render the current profile + review suggestions into the drafting user turn.

    `user_notes` carries the barista's own feedback (taste, preferences, constraints).
    `previous_draft` is a prior pending edit being refined — Claude should treat it
    as the starting point and adjust it per the notes, not start over.
    """
    parts = [
        "CURRENT PROFILE (the machine ran this):\n"
        + json.dumps(base_profile, indent=2, default=str),
        "REVIEW SUGGESTIONS to implement (profile_changes only):\n"
        + json.dumps(review["suggestions"], indent=2, default=str),
    ]
    if previous_draft is not None:
        parts.append(
            "PREVIOUS DRAFT (your earlier attempt — refine THIS per the barista notes, "
            "keeping its other changes intact):\n"
            + json.dumps(previous_draft.get("profile", {}), indent=2, default=str)
            + "\nIts change summary was:\n"
            + str(previous_draft.get("change_summary", ""))
        )
    if user_notes:
        parts.append("BARISTA NOTES (from the human tasting the coffee):\n" + user_notes)
    parts.append("Return the complete modified profile.")
    return "\n\n".join(parts)


STARTING_SYSTEM_PROMPT = """\
You are an expert espresso barista giving a barista a STARTING POINT for a bean \
they have never pulled before — a first shot to dial in from, not a finished \
recipe. You are given the bean (name/origin, roast level, process, roast date), \
the grinder, and — where available — the barista's most SIMILAR past shots: the \
telemetry, the profile each ran, and the review you gave. Anchor on those.

Method:
- If similar past shots are provided, START from what already worked for them — \
especially the higher-scoring ones — and nudge for the difference in roast level, \
process, or freshness. A shot that scored well on a similar bean is the best \
possible starting point; do not throw it away and start from theory.
- If no similar shots are provided, reason from first principles for the roast \
level: lighter roasts want a finer grind, higher temperature (~93–96°C), a longer \
ratio (~1:2 to 1:3) and a gentle, longer pre-infusion to avoid channeling; darker \
roasts want a coarser grind, lower temperature (~88–92°C), a shorter ratio \
(~1:1.5 to 1:2) and less pre-infusion. Very fresh beans (roasted in the last few \
days) degas heavily — expect faster flow and lean slightly finer.
- Give the grind setting in the grinder's OWN terms (its steps, clicks, numbers, \
or rotation marks); respect whether it is stepped or stepless. If no grinder is \
described, use generic "steps on a typical stepped grinder" language.
- Recommend a dose (g), a target yield (g), and the ratio. If the barista gave a \
dose, use it.
- Produce a COMPLETE, push-ready profile: a pre-infusion phase and one or more \
brew phases, each with temperature, pump target (pressure or flow), transition, \
and stop conditions (targets). Include a stop condition that ends the shot near \
the target yield (a volumetric/pumped target), so the shot terminates by weight.

Stay conservative and in safe bounds: water/phase temperature 60–96°C, pump \
pressure 0–12 bar, flow ≥ 0, each phase duration > 0 and ≤ 120s, phase type \
'preinfusion' or 'brew'. This is a place to START — favour a sensible, forgiving \
shot over an aggressive one. Never invent past-shot data that wasn't given.\
"""


class StartingPoint(BaseModel):
    """A first-shot recommendation for a brand-new bean, with a push-ready profile."""

    grind_setting: str = Field(
        description="Where to set the grinder, in ITS own terms (steps/clicks/numbers)."
    )
    dose_g: float = Field(description="Recommended dose in grams.")
    yield_g: float = Field(description="Target yield (out weight) in grams.")
    ratio: str = Field(description="Brew ratio, e.g. '1:2.2'.")
    rationale: str = Field(
        description="Why this starting point — cite the similar past shots when they informed it."
    )
    profile: DraftedProfile = Field(
        description="The complete push-ready starting profile (label, temperature, phases)."
    )


def build_starting_message(
    bean: dict[str, Any],
    grinder: Optional[str] = None,
    dose_target: Optional[float] = None,
    similar: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Render the new-bean context (+ similar past shots) into the user turn.

    `bean` is a structured bean dict (name, roast_level, process, roast_date).
    `similar` is a list of {shot, review, profile, similarity} for the closest
    past shots — their telemetry, the profile they ran, and the review given, so
    Claude can start from what worked rather than from theory.
    """
    parts = [
        "NEW BEAN (never pulled before — give a starting point to dial in from):",
        json.dumps(
            {
                "name": bean.get("name"),
                "roast_level": bean.get("roast_level"),
                "process": bean.get("process"),
                "roast_date": bean.get("roast_date"),
                "notes": bean.get("notes"),
            },
            indent=2,
            default=str,
        ),
    ]
    if grinder:
        parts.append(f"GRINDER: {grinder}")
    if dose_target:
        parts.append(f"DOSE the barista wants to use: {dose_target}g")
    if similar:
        blocks = []
        for s in similar:
            shot = s["shot"]
            t = shot.get("transformed", {})
            lines = [
                f"--- Similar past shot {shot['id']} "
                f"(bean: {shot.get('coffee') or 'unknown'}; similarity: {s.get('similarity_label', 'some')}) ---",
                "telemetry: "
                + json.dumps(
                    {
                        "duration_seconds": t.get("duration_seconds"),
                        "final_weight_g": t.get("final_weight_g"),
                        "summary": t.get("summary"),
                    },
                    default=str,
                ),
            ]
            if shot.get("tasting_notes"):
                lines.append(f"barista tasting notes: {shot['tasting_notes']}")
            if s.get("review"):
                lines.append("your review then: " + _compact_review(s["review"]))
            if s.get("profile"):
                lines.append(
                    "profile it ran (a strong basis for the starting profile):\n"
                    + json.dumps(s["profile"], indent=2, default=str)
                )
            blocks.append("\n".join(lines))
        parts.append(
            "MOST SIMILAR PAST SHOTS (start from what worked here, adjust for the "
            "roast-level/process difference):\n" + "\n\n".join(blocks)
        )
    else:
        parts.append(
            "No sufficiently similar past shots were found — reason from first "
            "principles for this roast level."
        )
    parts.append(
        "Return a starting grind, dose, yield/ratio, and a complete push-ready profile."
    )
    return "\n\n".join(parts)


def _compact_review(suggestions: dict[str, Any]) -> str:
    """One-paragraph summary of a past review's advice, for interleaving as context."""
    parts = []
    if suggestions.get("score") is not None:
        parts.append(f"scored it {suggestions['score']}/10")
    if suggestions.get("diagnosis"):
        parts.append(f"diagnosis: {suggestions['diagnosis']}")
    if suggestions.get("grind_change") and suggestions["grind_change"] != "none":
        parts.append(f"grind: {suggestions['grind_change']}")
    if suggestions.get("dose_yield_change") and suggestions["dose_yield_change"] != "none":
        parts.append(f"dose/yield: {suggestions['dose_yield_change']}")
    # Cap the profile changes: this compact line is interleaved into every later
    # review's context, so an edit with many changes shouldn't bloat the window.
    changes = suggestions.get("profile_changes") or []
    for pc in changes[:4]:
        parts.append(f"profile: {pc.get('parameter')} → {pc.get('change')}")
    if len(changes) > 4:
        parts.append(f"(+{len(changes) - 4} more profile changes)")
    return "; ".join(parts) if parts else "no changes recommended"


def build_user_message(
    shots: list[dict[str, Any]],
    grinder: Optional[str] = None,
    coffee: Optional[str] = None,
    prior_reviews: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """Render the recent-shots context into the user turn.

    `shots` is newest-first; we label them so Claude knows the ordering.
    `grinder` is the barista's free-text description of their grinder, so grind
    advice can be given in that grinder's own steps/clicks/numbers.
    `coffee` describes the beans (roast level, roast date) so advice fits them.
    Shots may also carry their own `coffee` (stamped at ingest / edited later);
    a per-shot line is emitted when it differs from the session coffee, so a
    bean change mid-window is visible.
    `prior_reviews` maps shot id → that review's suggestions, letting Claude see
    what it advised after each older shot and whether the advice worked.
    """
    if not shots:
        return "No shots are available to review."
    lines = [
        f"Here are the {len(shots)} most recent shots, newest first. "
        "Review them and recommend adjustments for the next shot.\n"
    ]
    if grinder:
        lines.append(f"GRINDER: {grinder}\n")
    if coffee:
        lines.append(f"COFFEE: {coffee}\n")
    for idx, shot in enumerate(shots):
        label = "most recent" if idx == 0 else f"{idx} shot(s) earlier"
        lines.append(f"=== Shot {shot['id']} ({label}) ===")
        lines.append(json.dumps(shot["transformed"], indent=2, default=str))
        if shot.get("coffee") and shot["coffee"] != coffee:
            lines.append(f"COFFEE (this shot): {shot['coffee']}")
        if shot.get("tasting_notes"):
            lines.append(f"TASTING NOTES (from the barista, on this shot): {shot['tasting_notes']}")
        if prior_reviews and idx > 0 and shot["id"] in prior_reviews:
            lines.append(
                "REVIEW GIVEN AFTER THIS SHOT: " + _compact_review(prior_reviews[shot["id"]])
            )
        lines.append("")
    return "\n".join(lines)
