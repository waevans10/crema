"""Offline smoke tests — no machine, no API key, no network.

These cover the pure pieces of the review/draft pipeline: building the prompt
that goes to Claude, the structured shapes Claude's responses are validated
against, and the device-safe clamping applied to a drafted profile before it
could ever be pushed. If these pass, the plumbing around the LLM call is sound.

Run with:  uv run pytest   (or: pytest)
"""

from __future__ import annotations

import datetime

from crema.draft import _clamp, _to_device_phase, diff_stop_conditions
from crema.prompts import (
    DraftedProfile,
    ReviewResult,
    build_draft_message,
    build_user_message,
)
from crema.web.app import _fmt_qty, _fmt_shot_time


def test_build_user_message_no_shots():
    assert build_user_message([]) == "No shots are available to review."


def test_build_user_message_labels_newest_first():
    shots = [
        {"id": "shot-a", "transformed": {"time": 28.0}},
        {"id": "shot-b", "transformed": {"time": 31.0}},
    ]
    msg = build_user_message(shots)
    # Both shots appear, newest is labelled "most recent", and ordering is shown.
    assert "shot-a" in msg and "shot-b" in msg
    assert "most recent" in msg
    assert "2 most recent shots" in msg


def test_review_result_validates_a_well_formed_review():
    review = ReviewResult.model_validate(
        {
            "score": 7,
            "diagnosis": "Even extraction, slightly fast.",
            "grind_change": "grind 1 step finer",
            "dose_yield_change": "none",
            "profile_changes": [],
            "confidence": "medium",
            "rationale": "Flow ramps smoothly; time is a touch short.",
        }
    )
    assert review.score == 7
    assert review.profile_changes == []


def test_fmt_shot_time_formats_and_falls_back():
    ts = int(datetime.datetime(2026, 7, 4, 14, 32).timestamp())
    # Device capture time is used and rendered as local date + time.
    assert _fmt_shot_time({"captured_at": ts, "transformed": {}}).endswith("14:32")
    # Falls back to the timestamp inside the transformed JSON.
    assert _fmt_shot_time({"captured_at": None, "transformed": {"timestamp": ts}}).endswith("14:32")
    # No clock at all -> a clear placeholder, never a crash.
    assert _fmt_shot_time({"captured_at": None, "transformed": {}}) == "time unknown"


def test_clamp_bounds():
    assert _clamp(5.0, 0.0, 10.0) == 5.0
    assert _clamp(-3.0, 0.0, 10.0) == 0.0
    assert _clamp(99.0, 0.0, 10.0) == 10.0


def test_diff_stop_conditions_flags_changes_and_ignores_identical():
    """Stop-condition changes must be detected so the UI can force acknowledgement."""
    base = {
        "phases": [
            {"name": "Preinfusion", "targets": [{"type": "pumped", "operator": "gte", "value": 60}]},
            {"name": "Extraction", "targets": [{"type": "volumetric", "operator": "gte", "value": 36}]},
        ]
    }
    identical = [dict(p) for p in base["phases"]]
    assert diff_stop_conditions(base, identical) == []

    changed = [
        {"name": "Preinfusion", "targets": []},
        {"name": "Extraction", "targets": [{"type": "volumetric", "operator": "gte", "value": 40}]},
    ]
    out = diff_stop_conditions(base, changed)
    assert any("removed stop" in c for c in out)
    assert any("36 → 40" in c for c in out)


def test_build_draft_message_includes_notes_and_previous_draft():
    base = {"label": "P", "phases": []}
    review = {"suggestions": {"diagnosis": "d"}}
    msg = build_draft_message(
        base, review,
        user_notes="tasted sour, keep preinfusion under 6s",
        previous_draft={"profile": {"label": "P"}, "change_summary": "raised temp"},
    )
    assert "BARISTA NOTES" in msg and "tasted sour" in msg
    assert "PREVIOUS DRAFT" in msg and "raised temp" in msg


def test_build_user_message_includes_grinder_when_set():
    shots = [{"id": "1", "transformed": {}}]
    assert "GRINDER: Niche Zero" in build_user_message(shots, grinder="Niche Zero")
    assert "GRINDER" not in build_user_message(shots)


def test_fmt_qty_handles_missing_values():
    assert _fmt_qty(44.2, "s") == "44.2s"
    assert _fmt_qty(None, "g") == "—"  # the old code rendered "Noneg"


def test_to_device_phase_clamps_unsafe_values():
    """A drafted phase with out-of-range values must be clamped to safe bounds."""
    drafted = DraftedProfile.model_validate(
        {
            "label": "Test",
            "temperature": 200.0,  # absurd; clamped elsewhere
            "change_summary": "raise temp",
            "phases": [
                {
                    "name": "Extraction",
                    "phase": "brew",
                    "duration": 999.0,  # over the 120s cap
                    "temperature": 130.0,  # over the 96C cap
                    "pump": {"target": "pressure", "pressure": 20.0, "flow": -1.0},
                    "targets": [],
                }
            ],
        }
    )
    phase = _to_device_phase(drafted.phases[0])
    assert phase["duration"] <= 120.0
    assert phase["temperature"] <= 96.0
    assert phase["pump"]["pressure"] <= 12.0
    assert phase["pump"]["flow"] >= 0.0
