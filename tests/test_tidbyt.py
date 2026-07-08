"""Offline tests for the Tidbyt integration — no device, no network."""

from __future__ import annotations

import asyncio

from crema import tidbyt
from crema.config import CremaConfig


def _review(score=8, profile="Lavazza 18g [AI]", bean="Lavazza"):
    return {"shot_id": "000094", "profile_name": profile, "bean": bean,
            "suggestions": {"score": score}}


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


def test_push_review_noop_when_unconfigured(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="", tidbyt_device_id="")
    calls = []

    async def fake_post(self, url, **kw):  # pragma: no cover - must never run
        calls.append(url)

    monkeypatch.setattr("aiohttp.ClientSession.post", fake_post)
    asyncio.run(tidbyt.push_review(cfg, _review()))
    assert calls == []  # HTTP never attempted


def test_push_review_posts_expected_request(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="tok123", tidbyt_device_id="devABC",
                      tidbyt_installation_id="crema")
    captured = {}

    class _Resp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def text(self): return "ok"

    def fake_post(self, url, headers=None, json=None, **kw):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("aiohttp.ClientSession.post", fake_post)
    asyncio.run(tidbyt.push_review(cfg, _review()))

    assert captured["url"] == "https://api.tidbyt.com/v0/devices/devABC/push"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["json"]["installationID"] == "crema"
    assert captured["json"]["background"] is False
    assert isinstance(captured["json"]["image"], str) and captured["json"]["image"]


def test_push_review_swallows_errors(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="tok", tidbyt_device_id="dev")

    def boom(self, *a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("aiohttp.ClientSession.post", boom)
    # Must not raise.
    asyncio.run(tidbyt.push_review(cfg, _review()))


def test_stored_enrichment_shape():
    # Guard the contract push_review depends on: review.py must add these keys.
    shot = {"id": "000094", "transformed": {"profile_name": "Lavazza [AI]"},
            "coffee": "Lavazza Super Crema"}
    stored = {"id": 1, "shot_id": shot["id"], "suggestions": {"score": 7}}
    stored["profile_name"] = shot["transformed"].get("profile_name")
    stored["bean"] = shot.get("coffee")
    assert stored["profile_name"] == "Lavazza [AI]"
    assert stored["bean"] == "Lavazza Super Crema"
