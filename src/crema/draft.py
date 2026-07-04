"""Draft a modified profile from a review, staged for human approval.

Loads the profile the reviewed shot actually ran (over WebSocket), asks the
stronger model to rewrite it per the review's profile suggestions, clamps the
result into device-safe bounds, validates it against the vendored profile schema,
and stores it as a *pending* edit. Nothing is written to the machine here — that
only happens on explicit approval (see push.py).
"""

from __future__ import annotations

from typing import Any, Optional

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


_OP = {"gte": "≥", "lte": "≤"}


def _fmt_val(v: Any) -> str:
    """Format a stop value canonically so 9, 9.0, and "9.0" all read '9'.

    Keeps numerically-equal values from being flagged as a stop-condition
    change just because the draft echoed a float for an int.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _fmt_target(t: dict[str, Any]) -> str:
    return f"{t.get('type', '?')} {_OP.get(str(t.get('operator')), t.get('operator', '?'))} {_fmt_val(t.get('value', '?'))}"


def diff_stop_conditions(
    base_profile: dict[str, Any], drafted_phases: list[dict[str, Any]]
) -> list[str]:
    """Human-readable list of stop-condition (targets) changes vs the base profile.

    Stop conditions decide when a phase — and often the whole shot — ends (by
    volume pumped, flow, or pressure). Changing them silently would change how the
    barista's shots terminate, so any difference found here is surfaced in the UI
    and must be explicitly acknowledged before the edit can be pushed.
    """
    changes: list[str] = []
    base_phases = base_profile.get("phases") or []
    for i in range(max(len(base_phases), len(drafted_phases))):
        base_ph = base_phases[i] if i < len(base_phases) else {}
        new_ph = drafted_phases[i] if i < len(drafted_phases) else {}
        name = new_ph.get("name") or base_ph.get("name") or f"phase {i + 1}"
        old_ts = list(base_ph.get("targets") or [])
        new_ts = list(new_ph.get("targets") or [])
        old = [_fmt_target(t) for t in old_ts]
        new = [_fmt_target(t) for t in new_ts]
        if old == new:
            continue
        # Pair up same-kind stops (type+operator) whose value changed, for a
        # cleaner "36 → 40" line; report the rest as added/removed.
        old_left, new_left = old.copy(), new.copy()
        for ot in old_ts:
            for nt in new_ts:
                if (
                    ot.get("type") == nt.get("type")
                    and ot.get("operator") == nt.get("operator")
                    and _fmt_val(ot.get("value")) != _fmt_val(nt.get("value"))
                    and _fmt_target(ot) in old_left
                    and _fmt_target(nt) in new_left
                ):
                    changes.append(
                        f"{name}: stop {ot.get('type')} "
                        f"{_OP.get(str(ot.get('operator')), '?')} "
                        f"{_fmt_val(ot.get('value'))} → {_fmt_val(nt.get('value'))}"
                    )
                    old_left.remove(_fmt_target(ot))
                    new_left.remove(_fmt_target(nt))
                    break
        for t in new_left:
            if t not in old_left:
                changes.append(f"{name}: added stop {t}")
        for t in old_left:
            if t not in new_left:
                changes.append(f"{name}: removed stop {t}")
    return changes


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


async def draft_from_review(
    conn: aiosqlite.Connection,
    config: CremaConfig,
    review_id: int,
    profile_id: Optional[str] = None,
    user_notes: Optional[str] = None,
    refine_edit_id: Optional[int] = None,
) -> dict[str, Any]:
    """Draft a profile edit for a stored review. Returns the pending-edit dict.

    `profile_id` chooses which profile to base the edit on — useful when the
    reviewed shots span several profiles. If omitted, the profile the review's
    newest shot ran on is used.

    `user_notes` is the barista's own feedback (taste, preferences, constraints),
    passed to Claude alongside the review. `refine_edit_id` refines an existing
    draft: that edit's profile is given to Claude as the starting point and the
    old draft is discarded once the new one is stored.

    Raises RuntimeError if the base profile can't be loaded or the draft is invalid.
    """
    review = await db.get_review(conn, review_id)
    if review is None:
        raise RuntimeError(f"Review {review_id} not found.")

    previous: Optional[dict[str, Any]] = None
    if refine_edit_id is not None:
        previous = await db.get_pending_edit(conn, refine_edit_id)
        if previous is None or previous["status"] != "draft":
            raise RuntimeError(f"Edit {refine_edit_id} is not an awaiting-approval draft.")
        # Refine against the same base profile the draft was built on.
        profile_id = profile_id or previous["base_profile_id"]

    if not profile_id:
        shot = await db.get_shot(conn, review["shot_id"])
        profile_id = (shot or {}).get("transformed", {}).get("profile_id") if shot else None
    if not profile_id:
        raise RuntimeError(
            "Can't draft: no profile selected and the reviewed shot has no profile_id."
        )
    profile_id = str(profile_id)

    # Prefer the profile cached during ingest — this lets drafting work with the
    # machine off. Only reach out to the machine if we've never cached it.
    base_profile = await db.get_profile(conn, profile_id)
    if base_profile is None:
        try:
            ws = GaggimateWebSocketClient(config.gaggimate())
            base_profile = await ws.load_profile(profile_id)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Profile {profile_id} isn't cached yet and the machine can't be reached "
                "to load it. Turn the machine on and run a review/ingest once to cache it."
            ) from e
        if base_profile:
            await db.upsert_profile(conn, profile_id, base_profile.get("label"), base_profile)
    if not base_profile:
        raise RuntimeError(f"Base profile {profile_id} could not be loaded.")
    base_label = base_profile.get("label") or base_profile.get("name") or profile_id

    client = AsyncAnthropic()
    response = await client.messages.parse(
        model=config.draft_model,
        # Headroom for a full profile rewrite plus any thinking tokens (which
        # count against max_tokens on adaptive-thinking models).
        max_tokens=8192,
        system=DRAFT_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": build_draft_message(
                    base_profile, review, user_notes=user_notes, previous_draft=previous
                ),
            }
        ],
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
    # Stop conditions decide when the shot ends — never let a draft change them
    # silently. Any difference vs the base profile is stored on the edit and must
    # be explicitly acknowledged in the UI before the edit can be pushed.
    stop_changes = diff_stop_conditions(base_profile, phases)
    edit_id = await db.insert_pending_edit(
        conn,
        label=profile_json["label"],
        change_summary=drafted.change_summary,
        profile=profile_json,
        review_id=review_id,
        base_profile_id=str(profile_id),
        base_profile_label=base_label,
        notes=user_notes,
        stop_changes=stop_changes,
    )
    if previous is not None:
        # The refined draft supersedes the old one.
        await db.set_edit_status(conn, previous["id"], "discarded")
    edit = await db.get_pending_edit(conn, edit_id)
    assert edit is not None
    return edit
