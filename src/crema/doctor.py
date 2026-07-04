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

    # 2. Device HTTP — the shot-history endpoint.
    try:
        idx = await GaggimateHTTPClient(config.gaggimate()).list_recent_shots(limit=1)
        checks.append(Check("Device HTTP (shots)", True, f"reachable — {len(idx)} shot(s) in index"))
    except Exception as e:  # noqa: BLE001 — report any failure verbatim
        checks.append(Check("Device HTTP (shots)", False, str(e)))

    # 3. Device WebSocket — profile list (also the push-back channel).
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
