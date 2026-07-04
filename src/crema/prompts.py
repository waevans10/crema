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
where available, the dose, yield, grind setting, and the taster's notes/rating.

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

Also give the most recent shot a 1-10 quality score: 1-3 badly flawed (gushing, \
choked, severe channeling), 4-6 drinkable but clearly off, 7-8 good, 9-10 dialled \
in. Judge it from the telemetry (extraction time, flow/pressure shape, channeling \
risk, temperature stability), not from taste you can't see.\
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


def build_user_message(shots: list[dict[str, Any]], grinder: Optional[str] = None) -> str:
    """Render the recent-shots context into the user turn.

    `shots` is newest-first; we label them so Claude knows the ordering.
    `grinder` is the barista's free-text description of their grinder, so grind
    advice can be given in that grinder's own steps/clicks/numbers.
    """
    if not shots:
        return "No shots are available to review."
    lines = [
        f"Here are the {len(shots)} most recent shots, newest first. "
        "Review them and recommend adjustments for the next shot.\n"
    ]
    if grinder:
        lines.append(f"GRINDER: {grinder}\n")
    for idx, shot in enumerate(shots):
        label = "most recent" if idx == 0 else f"{idx} shot(s) earlier"
        lines.append(f"=== Shot {shot['id']} ({label}) ===")
        lines.append(json.dumps(shot["transformed"], indent=2, default=str))
        lines.append("")
    return "\n".join(lines)
