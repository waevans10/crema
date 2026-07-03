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
choked shot that never reaches flow points to too fine. Prefer changing ONE variable \
at a time (usually grind first), and only suggest a profile change when grind/dose \
cannot address the problem.

Be concrete and specific: name the direction and rough magnitude of each change \
("grind 1–2 steps finer", "drop brew temperature ~1°C", "extend the pre-infusion \
phase by ~2s"). Stay within safe bounds (temperature 25–100°C, pressure 0–12 bar). \
If the shots already look good, say so and suggest nothing. Never invent data that \
isn't in the telemetry.\
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


def build_user_message(shots: list[dict[str, Any]]) -> str:
    """Render the recent-shots context into the user turn.

    `shots` is newest-first; we label them so Claude knows the ordering.
    """
    if not shots:
        return "No shots are available to review."
    lines = [
        f"Here are the {len(shots)} most recent shots, newest first. "
        "Review them and recommend adjustments for the next shot.\n"
    ]
    for idx, shot in enumerate(shots):
        label = "most recent" if idx == 0 else f"{idx} shot(s) earlier"
        lines.append(f"=== Shot {shot['id']} ({label}) ===")
        lines.append(json.dumps(shot["transformed"], indent=2, default=str))
        lines.append("")
    return "\n".join(lines)
