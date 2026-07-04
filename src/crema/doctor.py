"""Connectivity self-check: device HTTP, device WebSocket, and Claude API.

Each check is best-effort and reports a clear pass/fail so you can tell exactly
which link is broken without reading a stack trace.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from anthropic import AsyncAnthropic

from gaggimate_mcp.api.http import GaggimateHTTPClient
from gaggimate_mcp.api.websocket import GaggimateWebSocketClient

from .config import CremaConfig


class Check(NamedTuple):
    name: str
    ok: bool
    detail: str


async def run_checks(config: CremaConfig) -> list[Check]:
    checks: list[Check] = []

    # 1. API key present in the environment (after .env is loaded).
    key = os.environ.get("ANTHROPIC_API_KEY")
    checks.append(
        Check(
            "Anthropic API key",
            bool(key),
            "found in environment" if key else "ANTHROPIC_API_KEY not set (is it in .env?)",
        )
    )

    # 2 & 3. Device checks, per machine adapter.
    if config.machine == "gaggiuino":
        import aiohttp

        from .gaggiuino import _extract_latest_id, _get_json

        base = config.gaggiuino_url.rstrip("/")
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                latest = _extract_latest_id(await _get_json(session, f"{base}/api/shots/latest"))
            checks.append(Check("Gaggiuino HTTP (shots)", True, f"reachable — latest shot id {latest}"))
        except Exception as e:  # noqa: BLE001 — report any failure verbatim
            checks.append(Check("Gaggiuino HTTP (shots)", False, str(e)))
        checks.append(
            Check(
                "Profile push-back",
                True,
                "n/a on gaggiuino — reviews/notes/sharing only (drafting is GaggiMate-only)",
            )
        )
    else:
        # Device HTTP — the shot-history endpoint.
        try:
            idx = await GaggimateHTTPClient(config.gaggimate()).list_recent_shots(limit=1)
            checks.append(Check("Device HTTP (shots)", True, f"reachable — {len(idx)} shot(s) in index"))
        except Exception as e:  # noqa: BLE001 — report any failure verbatim
            checks.append(Check("Device HTTP (shots)", False, str(e)))

        # Device WebSocket — profile list (also the push-back channel).
        try:
            profiles = await GaggimateWebSocketClient(config.gaggimate()).list_profiles()
            checks.append(Check("Device WebSocket (profiles)", True, f"reachable — {len(profiles)} profile(s)"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("Device WebSocket (profiles)", False, str(e)))

    # 4. Claude API — validate auth + model with a cheap GET (no generation cost).
    if key:
        try:
            model = await AsyncAnthropic().models.retrieve(config.review_model)
            checks.append(Check("Claude API + model", True, f"auth OK — '{model.id}' reachable"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("Claude API + model", False, str(e)))
    else:
        checks.append(Check("Claude API + model", False, "skipped — no API key"))

    return checks
