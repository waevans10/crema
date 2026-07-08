"""Offline tests for the Tidbyt integration — no device, no network."""

from __future__ import annotations

from crema import tidbyt


def test_score_color_bands():
    assert tidbyt._score_color(2) == (192, 57, 43)    # red  (<4)
    assert tidbyt._score_color(5) == (194, 135, 26)   # amber (<7)
    assert tidbyt._score_color(9) == (63, 143, 67)    # green (>=7)
    assert tidbyt._score_color(None) == (120, 120, 120)  # neutral


def test_render_frame_returns_webp_bytes():
    data = tidbyt.render_frame(8, "Lavazza Super Crema 18g Latte [AI]", bean="Lavazza")
    assert isinstance(data, bytes) and len(data) > 0
    # RIFF....WEBP container signature
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def test_render_frame_tolerates_missing_fields():
    # No score, no profile, stale flag — must not raise and still produce a frame.
    data = tidbyt.render_frame(None, None, stale=True)
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
