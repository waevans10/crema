"""Offline smoke tests — no machine, no API key, no network.

These cover the pure pieces of the review/draft pipeline: building the prompt
that goes to Claude, the structured shapes Claude's responses are validated
against, and the device-safe clamping applied to a drafted profile before it
could ever be pushed. If these pass, the plumbing around the LLM call is sound.

Run with:  uv run pytest   (or: pytest)
"""

from __future__ import annotations

import asyncio
import datetime

from crema import db as crema_db
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

    # 9 vs 9.0 (and "9.0" as a string) are the SAME stop — no false positive.
    # Seen live: Draft #4 flagged "added ≥ 9.0 / removed ≥ 9" for an echo.
    echoed = [
        {"name": "Preinfusion", "targets": [{"type": "pumped", "operator": "gte", "value": 60.0}]},
        {"name": "Extraction", "targets": [{"type": "volumetric", "operator": "gte", "value": "36.0"}]},
    ]
    assert diff_stop_conditions(base, echoed) == []


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


def test_build_user_message_includes_tasting_notes_when_set():
    shots = [
        {"id": "shot-a", "transformed": {"time": 28.0}, "tasting_notes": "sour and thin"},
        {"id": "shot-b", "transformed": {"time": 31.0}},
    ]
    msg = build_user_message(shots)
    assert "TASTING NOTES" in msg
    assert "sour and thin" in msg
    # Only the shot that has notes gets a notes line.
    assert msg.count("TASTING NOTES") == 1


def test_build_user_message_includes_grinder_when_set():
    shots = [{"id": "1", "transformed": {}}]
    assert "GRINDER: Niche Zero" in build_user_message(shots, grinder="Niche Zero")
    assert "GRINDER" not in build_user_message(shots)


def test_build_user_message_includes_coffee_when_set():
    shots = [{"id": "1", "transformed": {}}]
    assert "COFFEE: light roast" in build_user_message(shots, coffee="light roast")
    assert "COFFEE" not in build_user_message(shots)


def test_build_user_message_per_shot_coffee_only_when_it_differs():
    shots = [
        {"id": "a", "transformed": {}, "coffee": "dark blend"},   # differs → shown
        {"id": "b", "transformed": {}, "coffee": "light roast"},  # same as session → omitted
        {"id": "c", "transformed": {}},                            # none → omitted
    ]
    msg = build_user_message(shots, coffee="light roast")
    assert "COFFEE (this shot): dark blend" in msg
    assert msg.count("COFFEE (this shot)") == 1
    # Without a session coffee, any per-shot coffee is shown.
    msg = build_user_message(shots)
    assert msg.count("COFFEE (this shot)") == 2


def test_build_user_message_interleaves_prior_reviews_for_older_shots_only():
    shots = [
        {"id": "shot-new", "transformed": {}},
        {"id": "shot-old", "transformed": {}},
    ]
    prior = {
        # Even if the newest shot has a past review, it must NOT be shown
        # (avoids anchoring the new review on the old opinion of the same shot).
        "shot-new": {"diagnosis": "should not appear", "grind_change": "none"},
        "shot-old": {"score": 4, "diagnosis": "gushing", "grind_change": "2 steps finer"},
    }
    msg = build_user_message(shots, prior_reviews=prior)
    assert "REVIEW GIVEN AFTER THIS SHOT" in msg
    assert "2 steps finer" in msg and "scored it 4/10" in msg
    assert "should not appear" not in msg


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


def test_tasting_notes_round_trip_and_migration(tmp_path):
    """Notes save/clear on a real DB file, and recent_shots surfaces them."""

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0})
            # Save, read back via both accessors.
            assert await crema_db.set_shot_tasting_notes(conn, "000001", "sour and thin")
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["tasting_notes"] == "sour and thin"
            recent = await crema_db.recent_shots(conn, limit=5)
            assert recent[0]["tasting_notes"] == "sour and thin"
            # Clear with None; unknown shot returns False.
            assert await crema_db.set_shot_tasting_notes(conn, "000001", None)
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["tasting_notes"] is None
            assert not await crema_db.set_shot_tasting_notes(conn, "999999", "nope")
        finally:
            await conn.close()

    asyncio.run(_run())


def test_latest_reviews_for_shots_returns_latest_per_shot(tmp_path):
    """Two reviews on one shot → the later one wins; unknown ids are absent."""

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0})
            await crema_db.insert_review(conn, "000001", "m", {"diagnosis": "first"})
            await crema_db.insert_review(conn, "000001", "m", {"diagnosis": "second"})
            out = await crema_db.latest_reviews_for_shots(conn, ["000001", "000002"])
            assert out["000001"]["diagnosis"] == "second"
            assert "000002" not in out
            assert await crema_db.latest_reviews_for_shots(conn, []) == {}
        finally:
            await conn.close()

    asyncio.run(_run())


def test_shot_coffee_stamped_on_insert_and_preserved_on_reingest(tmp_path):
    """Ingest stamps beans on first insert; a re-ingest never overwrites them."""

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0}, coffee="light roast")
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["coffee"] == "light roast"
            # Re-ingest with different beans in the hopper → original stamp kept.
            await crema_db.upsert_shot(conn, "000001", {"time": 29.0}, coffee="dark blend")
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["coffee"] == "light roast"
            assert shot["transformed"]["time"] == 29.0  # telemetry still refreshed
            # Barista edit wins; clearing works.
            assert await crema_db.set_shot_coffee(conn, "000001", "decaf test")
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["coffee"] == "decaf test"
        finally:
            await conn.close()

    asyncio.run(_run())


def test_export_bundle_shape_and_stable_install_id(tmp_path):
    """Bundle carries shots chronologically + reviews, and the install id persists."""
    from crema.config import CremaConfig
    from crema.export import build_export_bundle

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            cfg = CremaConfig(db_path=tmp_path / "crema.db")
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0}, captured_at=100.0, coffee="light roast")
            await crema_db.upsert_shot(conn, "000002", {"time": 31.0}, captured_at=200.0)
            await crema_db.set_shot_tasting_notes(conn, "000001", "sour")
            await crema_db.insert_review(conn, "000001", "m", {"diagnosis": "fast"})
            bundle = await build_export_bundle(conn, cfg)
            assert bundle["schema_version"] == 1
            # Chronological: oldest first, so advice->next-shot pairs read in order.
            assert [s["id"] for s in bundle["shots"]] == ["000001", "000002"]
            assert bundle["shots"][0]["coffee"] == "light roast"
            assert bundle["shots"][0]["tasting_notes"] == "sour"
            assert bundle["reviews"][0]["shot_id"] == "000001"
            assert bundle["reviews"][0]["suggestions"] == {"diagnosis": "fast"}
            # Same install id on a second export.
            again = await build_export_bundle(conn, cfg)
            assert again["install_id"] == bundle["install_id"]
            assert len(bundle["install_id"]) == 36  # uuid4
        finally:
            await conn.close()

    asyncio.run(_run())


def test_stamp_acceptance_records_terms_version_and_time():
    from crema.export import TERMS_VERSION, stamp_acceptance

    bundle = {"schema_version": 1}
    out = stamp_acceptance(bundle)
    assert out is bundle
    assert bundle["terms_version"] == TERMS_VERSION
    # ISO-8601 UTC timestamp.
    assert "T" in bundle["terms_accepted_at"] and "+00:00" in bundle["terms_accepted_at"]


def test_gaggiuino_transform_normalizes_x10_and_downsamples():
    """x10 fixed-point arrays become real units; long curves downsample to 24 pts."""
    from crema.gaggiuino import CURVE_POINTS, transform_gaggiuino_shot

    n = 300
    shot = {
        "id": 7,
        "timestamp": 1731316192,
        "duration": 315,  # 0.1s ticks -> 31.5s
        "datapoints": {
            "timeInShot": list(range(n)),
            "pressure": [92] * n,          # 9.2 bar
            "pumpFlow": [21] * n,          # 2.1 ml/s
            "shotWeight": list(range(0, n * 2, 2)),  # ends at 598 -> 59.8g... use last
            "temperature": [898] * n,      # 89.8 C
            "targetTemperature": [900] * n,
            "targetPressure": [90] * n,
            "targetPumpFlow": [20] * n,
        },
        "profile": {"id": 8, "name": "_Long", "waterTemperature": 90, "phases": []},
    }
    t = transform_gaggiuino_shot(shot)
    assert t["machine"] == "gaggiuino"
    assert t["shot_id"] == "000007"
    assert t["duration_seconds"] == 31.5
    assert t["peak_pressure_bar"] == 9.2
    assert t["avg_temperature_c"] == 89.8
    assert t["avg_target_temperature_c"] == 90.0
    assert t["avg_temp_deviation_c"] == -0.2
    assert t["profile_name"] == "_Long"
    assert t["final_weight_g"] == (n * 2 - 2) / 10
    assert len(t["curves"]["pressure_bar"]) == CURVE_POINTS
    assert t["curves"]["pressure_bar"][0] == 9.2


def test_gaggiuino_extract_latest_id_handles_shapes():
    from crema.gaggiuino import _extract_latest_id

    assert _extract_latest_id([{"lastShotId": 42}]) == 42
    assert _extract_latest_id({"lastShotId": "42"}) == 42
    assert _extract_latest_id({"id": 7}) == 7
    assert _extract_latest_id(13) == 13
    assert _extract_latest_id([]) is None
    assert _extract_latest_id({"nope": True}) is None


def test_resolve_share_url_override_and_off_switch(tmp_path):
    """Explicit CREMA_SHARE_URL wins (no network); 'off' disables sharing."""
    from crema.config import CremaConfig
    from crema.export import resolve_share_url

    async def _run() -> None:
        cfg = CremaConfig(db_path=tmp_path / "x.db", share_url="https://example.com/v1/bundles")
        assert await resolve_share_url(cfg) == "https://example.com/v1/bundles"
        for off in ("off", "OFF", "none", "disabled"):
            cfg = CremaConfig(db_path=tmp_path / "x.db", share_url=off)
            assert await resolve_share_url(cfg) is None

    asyncio.run(_run())
