"""Draft a modified profile from a review, staged for human approval.

Loads the profile the reviewed shot actually ran (over WebSocket), asks the
stronger model to rewrite it per the review's profile suggestions, clamps the
result into device-safe bounds, validates it against the vendored profile schema,
and stores it as a *pending* edit. Nothing is written to the machine here — that
only happens on explicit approval (see push.py).
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from gaggimate_mcp.api.websocket import GaggimateWebSocketClient
from gaggimate_mcp.models.profile import ProfileData

from . import db
from .config import CremaConfig
from .prompts import DRAFT_SYSTEM_PROMPT, DraftedProfile, build_draft_message

# Device-safe bounds (mirror the vendored profile schema).
TEMP_MIN, TEMP_MAX = 60.0, 96.0
PRESSURE_MIN, PRESSURE_MAX = 0.0, 12.0
DURATION_MIN, DURATION_MAX = 0.1, 120.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_device_phase(phase: Any) -> dict[str, Any]:
    """Map a DraftPhase to a device profile phase dict, clamping to safe bounds."""
    p: dict[str, Any] = {
        "name": phase.name,
        "phase": phase.phase,
        "duration": _clamp(phase.duration, DURATION_MIN, DURATION_MAX),
        "pump": {
            "target": phase.pump.target,
            "pressure": _clamp(phase.pump.pressure, PRESSURE_MIN, PRESSURE_MAX),
            "flow": max(0.0, phase.pump.flow),
        },
        "targets": [{"type": t.type, "operator": t.operator, "value": t.value} for t in phase.targets],
    }
    if phase.temperature is not None:
        p["temperature"] = _clamp(phase.temperature, TEMP_MIN, TEMP_MAX)
    if phase.transition is not None:
        p["transition"] = {"type": phase.transition.type, "duration": max(0.0, phase.transition.duration)}
    return p


async def draft_from_review(conn: aiosqlite.Connection, config: CremaConfig, review_id: int) -> dict[str, Any]:
    """Draft a profile edit for a stored review. Returns the pending-edit dict.

    Raises RuntimeError if the base profile can't be loaded or the draft is invalid.
    """
    review = await db.get_review(conn, review_id)
    if review is None:
        raise RuntimeError(f"Review {review_id} not found.")

    shot = await db.get_shot(conn, review["shot_id"])
    profile_id = (shot or {}).get("transformed", {}).get("profile_id") if shot else None
    if not profile_id:
        raise RuntimeError(
            "Can't draft: the reviewed shot has no profile_id, so there's no base profile to edit."
        )

    ws = GaggimateWebSocketClient(config.gaggimate())
    base_profile = await ws.load_profile(str(profile_id))
    if not base_profile:
        raise RuntimeError(f"Base profile {profile_id} could not be loaded from the machine.")
    base_label = base_profile.get("label") or base_profile.get("name") or str(profile_id)

    client = AsyncAnthropic()
    response = await client.messages.parse(
        model=config.draft_model,
        max_tokens=4096,
        system=DRAFT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_draft_message(base_profile, review)}],
        output_format=DraftedProfile,
    )
    drafted = response.parsed_output
    if drafted is None:
        raise RuntimeError(
            f"Claude did not return a structured profile (stop_reason={response.stop_reason})."
        )

    temperature = _clamp(drafted.temperature, TEMP_MIN, TEMP_MAX)
    phases = [_to_device_phase(ph) for ph in drafted.phases]

    # Validate against the vendored schema before we ever consider pushing it.
    try:
        ProfileData(
            name=drafted.label[:100] or base_label[:100],
            description=drafted.change_summary[:500],
            temperature=temperature,
            phases=phases,  # coerced into PhaseData/PumpSettings/etc.
        )
    except ValidationError as e:
        raise RuntimeError(f"Drafted profile failed validation and was not stored:\n{e}") from e

    profile_json = {
        "label": drafted.label or base_label,
        "type": base_profile.get("type", "pro"),
        "temperature": temperature,
        "phases": phases,
    }
    edit_id = await db.insert_pending_edit(
        conn,
        label=profile_json["label"],
        change_summary=drafted.change_summary,
        profile=profile_json,
        review_id=review_id,
        base_profile_id=str(profile_id),
        base_profile_label=base_label,
    )
    edit = await db.get_pending_edit(conn, edit_id)
    assert edit is not None
    return edit
