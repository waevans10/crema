"""Offline smoke tests — no machine, no API key, no network.

These cover the pure pieces of the review/draft pipeline: building the prompt
that goes to Claude, the structured shapes Claude's responses are validated
against, and the device-safe clamping applied to a drafted profile before it
could ever be pushed. If these pass, the plumbing around the LLM call is sound.

Run with:  uv run pytest   (or: pytest)
"""

from __future__ import annotations

import datetime

from crema.draft import _clamp, _to_device_phase
from crema.prompts import (
    DraftedProfile,
    ReviewResult,
    build_user_message,
)
from crema.web.app import _fmt_shot_time


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
