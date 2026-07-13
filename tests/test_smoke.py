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
import json

from crema import db as crema_db
from crema.draft import _clamp, _to_device_phase, diff_stop_conditions
from crema.prompts import (
    DraftedProfile,
    ReviewResult,
    build_draft_message,
    build_user_message,
)
from crema.scoring import execution_score
from crema.web.app import _fmt_qty, _fmt_shot_time, _render_review


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
            "score_reason": "Capped by a slightly short extraction; otherwise clean.",
            "diagnosis": "Even extraction, slightly fast.",
            "grind_change": "grind 1 step finer",
            "dose_yield_change": "none",
            "profile_changes": [],
            "confidence": "medium",
            "rationale": "Flow ramps smoothly; time is a touch short.",
        }
    )
    assert review.score == 7
    assert review.score_reason.startswith("Capped by")
    assert review.profile_changes == []


def _review_dict(**suggestions):
    base = {
        "score": 5,
        "score_reason": "Grind correction held; capped by 99.7°C brew temp (target 94°C).",
        "diagnosis": "Puck now behaving after the grind fix.",
        "grind_change": "none",
        "dose_yield_change": "none",
        "profile_changes": [],
        "confidence": "high",
        "rationale": "Resistance is healthy; temperature is the dominant flaw.",
    }
    base.update(suggestions)
    return {"id": 1, "shot_id": "000095", "model": "claude-sonnet-5", "created_at": None, "suggestions": base}


def test_render_review_shows_score_reason_when_present():
    html_out = _render_review(_review_dict(), profiles=[])
    assert "score-reason" in html_out
    assert "99.7°C" in html_out


def test_render_review_omits_score_reason_when_missing():
    review = _review_dict()
    del review["suggestions"]["score_reason"]  # older reviews predate the field
    html_out = _render_review(review, profiles=[])
    assert "score-reason" not in html_out


def test_profile_recommendation_requires_cup_rating_then_surfaces_draft():
    from crema.web.app import _profile_recommendation
    review = _review_dict(profile_changes=[{"parameter": "temperature", "change": "93°C"}])
    unrated = {"id": "000095", "cup_rating": None}
    assert "Rate the latest cup" in _profile_recommendation(review, unrated, [], [])
    rated = {"id": "000095", "cup_rating": 4}
    card = _profile_recommendation(review, rated, [], [])
    assert "Profile recommendation ready" in card and "Draft a profile edit" in card


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


def test_build_user_message_includes_structured_cup_rating():
    msg = build_user_message([{"id": "shot-a", "transformed": {}, "cup_rating": 4}])
    assert "BARISTA CUP RATING: 4/5" in msg


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


def test_cup_rating_round_trip_and_validation(tmp_path):
    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0})
            assert await crema_db.set_shot_cup_rating(conn, "000001", 4)
            shot = await crema_db.get_shot(conn, "000001")
            assert shot is not None and shot["cup_rating"] == 4
            assert await crema_db.set_shot_cup_rating(conn, "000001", None)
            assert (await crema_db.get_shot(conn, "000001"))["cup_rating"] is None
        finally:
            await conn.close()

    asyncio.run(_run())


def test_execution_score_is_deterministic_and_channeling_is_dominant():
    shot = {
        "diagnostics": {
            "channeling": {"channeling_risk": "VERY_HIGH"},
            "temperature": {"stability_std_c": 1.0},
            "profile_compliance": {"pressure_rmse_bar": 1.0, "flow_rmse_ml_s": 1.0},
            "resistance": {"annotations": {"erosion": "VERY_HIGH"}},
        }
    }
    score = execution_score(shot)
    assert score == execution_score(shot)  # no model or random input involved
    assert score["score"] < 5
    assert score["components"]["channeling"] < score["components"]["flow_adherence"]


def test_execution_score_marks_missing_diagnostics_as_low_confidence():
    score = execution_score({})
    assert score["score"] == 5.0
    assert score["confidence"] == "low"


def test_execution_score_applies_only_explicit_recipe_targets():
    shot = {"profile_id": "p1", "final_weight_g": 50.0, "diagnostics": {
        "channeling": {"channeling_risk": "LOW"},
        "temperature": {"stability_std_c": 0.1},
        "profile_compliance": {"pressure_rmse_bar": 0.1, "flow_rmse_ml_s": 0.1},
        "resistance": {"annotations": {"erosion": "LOW"}},
    }}
    baseline = execution_score(shot)
    recipe = execution_score(shot, {"target_yield_g": 36.0, "target_profile_id": "p2"})
    assert baseline["score"] == 10.0
    assert recipe["score"] < baseline["score"]
    assert "recipe_yield" in recipe["components"] and "recipe_profile" in recipe["components"]


def test_recipe_card_uses_the_profile_helper_display_name():
    from crema.web.app import _recipe_card
    html_out = _recipe_card(
        {"id": 1, "name": "Bean", "target_profile_id": "p1"},
        [{"id": "p1", "name": "Daily driver"}],
    )
    assert "Daily driver" in html_out and "selected" in html_out


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


def test_export_bundle_is_canonical_and_keeps_free_text_local(tmp_path):
    """Shared data is chronological and uniform, without personal free text."""
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
            assert bundle["schema_version"] == 2
            assert [s["shot_index"] for s in bundle["shots"]] == [1, 2]
            assert bundle["shots"][0]["outcome"] == {"cup_rating": None, "taste_tags": ["sour"]}
            assert bundle["reviews"][0]["shot_index"] == 1
            assert bundle["reviews"][0]["assistant_used"] is True
            # Names, raw notes, device shot ids, timestamps, and AI prose never leave the box.
            shared = json.dumps(bundle)
            assert "light roast" not in shared and '"sour"' in shared
            assert "000001" not in shared and "fast" not in shared
            # Same random participant id on a second export.
            again = await build_export_bundle(conn, cfg)
            assert again["participant_id"] == bundle["participant_id"]
            assert len(bundle["participant_id"]) == 36  # uuid4
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


def test_taste_hint_prefers_review_then_duration_heuristic():
    from crema.web.app import _taste_hint

    shot_fast = {"id": "1", "transformed": {"duration_seconds": 18.0}}
    shot_slow = {"id": "2", "transformed": {"duration_seconds": 49.3}}
    shot_mid = {"id": "3", "transformed": {"duration_seconds": 30.0}}
    shot_nodata = {"id": "4", "transformed": {}}

    # Review wins over the heuristic and carries the score.
    hint = _taste_hint(shot_fast, {"diagnosis": "Gushing, badly channeled.", "score": 3})
    assert "AI read" in hint and "3/10" in hint and "Gushing" in hint

    assert "sour side" in _taste_hint(shot_fast, None)
    assert "bitter side" in _taste_hint(shot_slow, None)
    assert "classic window" in _taste_hint(shot_mid, None)
    assert _taste_hint(shot_nodata, None) == ""


def test_taste_vocab_defs_cover_all_chips():
    """Every chip word must have a definition (legend + tooltip stay in sync)."""
    from crema.web.app import _TASTE_CHIPS, _TASTE_DEFS, _taste_chips_html, _taste_guide_html

    for _, words in _TASTE_CHIPS:
        for w in words:
            assert w in _TASTE_DEFS, f"missing definition for chip: {w}"
    chips = _taste_chips_html()
    assert "tchip(this)" in chips and "astringent" in chips
    guide = _taste_guide_html()
    assert "SWEET SPOT" in guide and "over-steeped black tea" in guide


def test_maybe_autoshare_gates_on_consent(tmp_path, monkeypatch):
    """Off -> None; stale terms -> paused message (no upload); consented -> uploads
    with the STORED acceptance stamp, not a fresh one."""
    from crema import export as export_mod
    from crema.config import CremaConfig
    from crema.export import TERMS_VERSION, maybe_autoshare, record_autoshare_consent

    sent: list[dict] = []

    async def fake_share(bundle, url):
        sent.append(bundle)
        return "ok"

    async def fake_resolve(cfg):
        return "https://example.com/v1/bundles"

    monkeypatch.setattr(export_mod, "share_bundle", fake_share)
    monkeypatch.setattr(export_mod, "resolve_share_url", fake_resolve)

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            cfg = CremaConfig(db_path=tmp_path / "crema.db")
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0})

            # Off by default: no upload, no message.
            assert await maybe_autoshare(conn, cfg) is None

            # On, but consent recorded for an older terms version: paused.
            await crema_db.set_setting(conn, "autoshare", "1")
            await crema_db.set_setting(conn, "autoshare_terms_version", "0")
            await crema_db.set_setting(conn, "autoshare_accepted_at", "2026-01-01T00:00:00+00:00")
            msg = await maybe_autoshare(conn, cfg)
            assert msg is not None and "paused" in msg and not sent

            # Proper opt-in: uploads, stamped with the stored consent moment.
            await record_autoshare_consent(conn)
            accepted = await crema_db.get_setting(conn, "autoshare_accepted_at")
            msg = await maybe_autoshare(conn, cfg)
            assert msg is not None and "auto-shared" in msg and len(sent) == 1
            assert sent[0]["terms_version"] == TERMS_VERSION
            assert sent[0]["terms_accepted_at"] == accepted
        finally:
            await conn.close()

    asyncio.run(_run())


# --- new-bean starting point --------------------------------------------------


def test_parse_roast_bucket_reads_compound_levels_first():
    from crema.starting import parse_roast_bucket

    assert parse_roast_bucket("medium-dark Brazilian") == 3  # not 'medium' or 'dark'
    assert parse_roast_bucket("medium light washed") == 1
    assert parse_roast_bucket("light roast Colombian") == 0
    assert parse_roast_bucket("dark Italian blend") == 4
    assert parse_roast_bucket("just some coffee") is None
    assert parse_roast_bucket(None) is None


def test_similarity_ranks_roast_then_origin():
    from crema.starting import similarity

    bean = {"name": "Colombian Huila", "roast_level": "light", "process": "washed"}
    exact = similarity(bean, "Colombian Huila · light roast · washed")
    same_roast_diff_origin = similarity(bean, "Ethiopian natural, light roast")
    adjacent = similarity(bean, "medium-light Ethiopian")  # adjacent roast, no origin overlap
    unrelated = similarity(bean, "dark Italian blend")
    # Exact beats same-roast-different-origin beats adjacent-roast beats unrelated.
    assert exact > same_roast_diff_origin > adjacent > unrelated == 0


def test_find_similar_shots_excludes_unrelated_and_ranks(tmp_path):
    """Only meaningfully-similar shots come back, best first, with their reviews."""
    from crema.starting import find_similar_shots

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"profile_id": "p"}, captured_at=1.0, coffee="dark Italian blend")
            await crema_db.upsert_shot(conn, "000002", {"profile_id": "p"}, captured_at=2.0, coffee="Ethiopian light roast")
            await crema_db.upsert_shot(conn, "000003", {"profile_id": "p"}, captured_at=3.0, coffee="Colombian Huila, light roast, washed")
            await crema_db.insert_review(conn, "000003", "m", {"score": 8, "diagnosis": "great"})
            bean = {"name": "Colombian Huila", "roast_level": "light", "process": "washed"}
            out = await find_similar_shots(conn, bean, limit=5)
            ids = [s["shot"]["id"] for s in out]
            assert ids[0] == "000003"          # closest first
            assert "000001" not in ids          # dark blend excluded
            assert out[0]["review"]["score"] == 8
        finally:
            await conn.close()

    asyncio.run(_run())


def test_bean_library_roundtrip_and_roast_check_constraint(tmp_path):
    import aiosqlite

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            bid = await crema_db.insert_bean(conn, "Colombian Huila", "light", process="washed", roast_date="2026-06-20")
            await crema_db.set_active_bean(conn, bid)
            active = await crema_db.active_bean(conn)
            assert active is not None and active["id"] == bid
            # Active bean keeps the freetext coffee setting in sync for reviews.
            assert await crema_db.get_setting(conn, "coffee") == crema_db.canonical_coffee(active)
            # The roast_level CHECK constraint rejects out-of-vocabulary values.
            try:
                await conn.execute("INSERT INTO beans (name, roast_level) VALUES ('x', 'extra-light')")
                raise AssertionError("CHECK constraint should have rejected 'extra-light'")
            except aiosqlite.IntegrityError:
                pass
        finally:
            await conn.close()

    asyncio.run(_run())


def test_score_history_pairs_scores_and_orders_oldest_first(tmp_path):
    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"final_weight_g": 36.0, "duration_seconds": 27.0}, captured_at=100.0)
            await crema_db.upsert_shot(conn, "000002", {"final_weight_g": 38.0, "duration_seconds": 30.0}, captured_at=200.0)
            await crema_db.insert_review(conn, "000002", "m", {"score": 8})
            hist = await crema_db.score_history(conn, limit=10)
            assert [h["id"] for h in hist] == ["000001", "000002"]  # oldest → newest
            assert hist[0]["score"] is None and hist[1]["score"] == 8
            assert hist[1]["yield_g"] == 38.0
        finally:
            await conn.close()

    asyncio.run(_run())


def test_recipe_and_experiment_capture_matching_followup_shots(tmp_path):
    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            bean_id = await crema_db.insert_bean(conn, "Test bean", "medium")
            assert await crema_db.set_bean_recipe(conn, bean_id, 18.0, 36.0, "profile-a", "sweet")
            bean = await crema_db.get_bean(conn, bean_id)
            assert bean["target_yield_g"] == 36.0 and bean["target_profile_id"] == "profile-a"
            await crema_db.upsert_shot(conn, "000001", {}, bean_id=bean_id)
            review_id = await crema_db.insert_review(conn, "000001", "m", {"score": 6})
            experiment_id = await crema_db.start_experiment(conn, review_id, bean_id, "1 click finer", 6, 3)
            await crema_db.upsert_shot(conn, "000002", {}, bean_id=bean_id)
            await crema_db.assign_shot_to_active_experiment(conn, "000002", bean_id)
            await crema_db.upsert_shot(conn, "000003", {}, bean_id=bean_id + 1)
            await crema_db.assign_shot_to_active_experiment(conn, "000003", bean_id + 1)
            active = await crema_db.active_experiment(conn)
            assert active is not None and active["id"] == experiment_id
            assert [s["id"] for s in active["shots"]] == ["000002"]
            assert await crema_db.close_experiment(conn, experiment_id)
            assert await crema_db.active_experiment(conn) is None
        finally:
            await conn.close()

    asyncio.run(_run())


def test_export_includes_structured_experiments_without_private_notes(tmp_path):
    from crema.config import CremaConfig
    from crema.export import build_export_bundle

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            bean_id = await crema_db.insert_bean(conn, "Private roaster name", "medium")
            await crema_db.upsert_shot(conn, "000001", {}, bean_id=bean_id)
            experiment_id = await crema_db.start_experiment(
                conn, None, bean_id, "My private grinder reference", 7, 3,
                variable="grind", direction="finer", magnitude=2, unit="grinder_steps",
            )
            await crema_db.upsert_shot(conn, "000002", {}, bean_id=bean_id)
            await crema_db.assign_shot_to_active_experiment(conn, "000002", bean_id)
            bundle = await build_export_bundle(conn, CremaConfig(db_path=tmp_path / "crema.db"))
            assert bundle["experiments"] == [{
                "experiment_index": 1, "variable": "grind", "direction": "finer",
                "magnitude": 2.0, "unit": "grinder_steps", "baseline_execution_score": 7,
                "baseline_cup_rating": 3, "followup_shot_indices": [2], "status": "active",
            }]
            assert "Private roaster name" not in json.dumps(bundle)
            assert "private grinder" not in json.dumps(bundle).lower()
            assert experiment_id == 1
        finally:
            await conn.close()

    asyncio.run(_run())


def test_dataset_importer_validates_references_and_writes_rows(tmp_path):
    from crema.dataset import import_bundle, validate_bundle

    bundle = {
        "schema_version": 2, "participant_id": "a" * 36, "machine": "gaggimate",
        "shots": [{"shot_index": 1, "relative_day": 0, "bean": {"bean_code": "bean_1", "roast_level": "medium", "process": "washed", "roast_age_days": 12}, "outcome": {"cup_rating": 4, "taste_tags": ["sweet"]}, "telemetry": {}}],
        "reviews": [{"shot_index": 1, "assistant_used": True, "confidence": "high", "execution_score": 8}],
        "experiments": [{"experiment_index": 1, "variable": "grind", "direction": "finer", "magnitude": 2, "unit": "grinder_steps", "baseline_execution_score": 7, "baseline_cup_rating": 3, "followup_shot_indices": [1], "status": "closed"}],
    }

    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "pool.db")
        try:
            await import_bundle(conn, bundle)
            async with conn.execute("SELECT count(*) AS n FROM community_shots") as cur:
                assert (await cur.fetchone())["n"] == 1
        finally:
            await conn.close()

    asyncio.run(_run())
    bad = dict(bundle, reviews=[dict(bundle["reviews"][0], shot_index=99)])
    try:
        validate_bundle(bad)
        raise AssertionError("invalid shot reference should fail")
    except ValueError:
        pass


def test_workflow_prompt_requires_context_then_cup_rating():
    from crema.web.app import _workflow_prompt
    assert "Set up this bean" in _workflow_prompt(None, None, None)
    shot = {"id": "1", "coffee": "bean", "tasting_notes": "", "cup_rating": None}
    assert "Save cup rating" in _workflow_prompt(shot, {"name": "bean"}, None)


def test_manual_experiment_outcomes_do_not_require_an_ai_review(tmp_path):
    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            bean_id = await crema_db.insert_bean(conn, "Test bean", "medium")
            telemetry = {"diagnostics": {
                "channeling": {"channeling_risk": "LOW"},
                "temperature": {"stability_std_c": 0.1},
                "profile_compliance": {"pressure_rmse_bar": 0.1, "flow_rmse_ml_s": 0.1},
                "resistance": {"annotations": {"erosion": "LOW"}},
            }}
            await crema_db.upsert_shot(conn, "000001", telemetry, bean_id=bean_id)
            await crema_db.start_experiment(conn, None, bean_id, "one click finer", 10, None)
            await crema_db.upsert_shot(conn, "000002", telemetry, bean_id=bean_id)
            await crema_db.assign_shot_to_active_experiment(conn, "000002", bean_id)
            active = await crema_db.active_experiment(conn)
            assert active is not None and active["shots"] == [{"id": "000002", "cup_rating": None, "score": 10}]
        finally:
            await conn.close()

    asyncio.run(_run())


def test_manual_guidance_is_local_and_offers_an_experiment():
    from crema.web.app import _manual_guidance
    html_out = _manual_guidance(
        {"id": "001", "transformed": {"diagnostics": None}}, None
    )
    assert "Read the shot yourself" in html_out
    assert "Start my experiment" in html_out
    assert "Claude" not in html_out


def test_learning_lesson_teaches_channeling_as_a_clue_not_a_verdict():
    from crema.web.app import _learning_lesson
    html_out = _learning_lesson({
        "cup_rating": None,
        "transformed": {"diagnostics": {"channeling": {"channeling_risk": "HIGH"}}},
    })
    assert "GaggiMate companion" in html_out
    assert "clue, not proof" in html_out
    assert "last third" in html_out


def test_learning_lesson_explains_intentional_profile_shape():
    from crema.web.app import _learning_lesson
    html_out = _learning_lesson({
        "cup_rating": 4,
        "transformed": {"diagnostics": {
            "channeling": {"channeling_risk": "LOW"},
            "temperature": {"undershoot_c": 0.2},
            "profile_compliance": {"flow_rmse_ml_s": 0.1, "pressure_rmse_bar": 0.1},
        }},
    })
    assert "may be intentional" in html_out
    assert "rated this cup 4/5" in html_out


def test_review_persists_token_usage(tmp_path):
    async def _run() -> None:
        conn = await crema_db.connect(tmp_path / "crema.db")
        try:
            await crema_db.upsert_shot(conn, "000001", {"time": 28.0})
            rid = await crema_db.insert_review(conn, "000001", "m", {"score": 7}, input_tokens=1234, output_tokens=567)
            row = await crema_db.get_review(conn, rid)
            assert row["input_tokens"] == 1234 and row["output_tokens"] == 567
        finally:
            await conn.close()

    asyncio.run(_run())


def test_state_sig_changes_on_new_review_or_shot():
    from crema.web.app import _state_sig

    base = _state_sig({"id": 5}, [{"id": "000010"}], 1)
    assert _state_sig({"id": 6}, [{"id": "000010"}], 1) != base   # new review
    assert _state_sig({"id": 5}, [{"id": "000011"}], 1) != base   # new shot
    assert _state_sig({"id": 5}, [{"id": "000010"}], 2) != base   # new edit
    assert _state_sig({"id": 5}, [{"id": "000010"}], 1) == base   # unchanged
    assert _state_sig(None, [], 0) == "0:-:0"                      # empty state


def test_bean_age_days_and_aging_banner():
    from crema.web.app import _bean_age_days, _aging_banner

    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    assert _bean_age_days(today) == 0
    assert _bean_age_days(old) == 90
    assert _bean_age_days(None) is None
    assert _aging_banner({"name": "X", "roast_level": "light", "roast_date": old})  # warns
    assert _aging_banner({"name": "X", "roast_level": "light", "roast_date": today}) == ""  # fresh
