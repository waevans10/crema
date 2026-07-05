"""Gaggiuino adapter: ingest shots from a Gaggiuino machine's REST API.

Gaggiuino (https://gaggiuino.github.io/) exposes shots over plain HTTP:

    GET /api/shots/latest   -> the newest shot id (ids are sequential ints)
    GET /api/shots/{id}     -> full shot: datapoint arrays + embedded profile

Datapoint arrays are x10 fixed-point ints (pressure 92 = 9.2 bar, temperature
898 = 89.8 C, timeInShot in 0.1s ticks). We normalize to real units and produce
a compact AI-friendly telemetry dict — the review pipeline doesn't require the
GaggiMate transformer's schema, just honest, well-labelled telemetry.

Reviews, tasting notes, beans, and the community pool all work on these shots.
Profile drafting/push-back stays GaggiMate-only for now (different write API).
"""

from __future__ import annotations

from typing import Any, Optional

import aiohttp
import aiosqlite

from . import db
from .config import CremaConfig

# Downsampled curve length for review context — enough to see shape without
# blowing up tokens.
CURVE_POINTS = 24


def _f(v: Any) -> Optional[float]:
    """x10 fixed-point int -> float, None-safe."""
    try:
        return round(float(v) / 10.0, 2)
    except (TypeError, ValueError):
        return None


def _downsample(values: list[Any], n: int = CURVE_POINTS) -> list[Optional[float]]:
    """Evenly sample a x10 array down to <= n normalized points."""
    if not values:
        return []
    if len(values) <= n:
        return [_f(v) for v in values]
    step = (len(values) - 1) / (n - 1)
    return [_f(values[round(i * step)]) for i in range(n)]


def transform_gaggiuino_shot(shot: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw /api/shots/{id} payload into review-ready telemetry."""
    dp = shot.get("datapoints") or {}
    profile = shot.get("profile") or {}

    time_ticks = dp.get("timeInShot") or []
    pressure = dp.get("pressure") or []
    flow = dp.get("pumpFlow") or []
    weight = dp.get("shotWeight") or []
    temp = dp.get("temperature") or []
    target_temp = dp.get("targetTemperature") or []

    duration_s = _f(shot.get("duration"))
    if duration_s is None and time_ticks:
        duration_s = _f(time_ticks[-1])

    pressures = [p / 10.0 for p in pressure if isinstance(p, (int, float))]
    temps = [t / 10.0 for t in temp if isinstance(t, (int, float))]
    target_temps = [t / 10.0 for t in target_temp if isinstance(t, (int, float))]

    transformed: dict[str, Any] = {
        "machine": "gaggiuino",
        "shot_id": str(shot.get("id", "")).zfill(6),
        "timestamp": shot.get("timestamp"),
        "duration_seconds": duration_s,
        "profile_id": str(profile.get("id")) if profile.get("id") is not None else None,
        "profile_name": profile.get("name"),
        "profile_target_temperature_c": profile.get("waterTemperature"),
        "final_weight_g": _f(weight[-1]) if weight else None,
        "peak_pressure_bar": round(max(pressures), 2) if pressures else None,
        "avg_pressure_bar": round(sum(pressures) / len(pressures), 2) if pressures else None,
        "avg_temperature_c": round(sum(temps) / len(temps), 2) if temps else None,
        "avg_target_temperature_c": (
            round(sum(target_temps) / len(target_temps), 2) if target_temps else None
        ),
        # Downsampled curves, aligned by index (~evenly spaced through the shot).
        "curves": {
            "time_s": _downsample(time_ticks),
            "pressure_bar": _downsample(pressure),
            "flow_mls": _downsample(flow),
            "weight_g": _downsample(weight),
            "temperature_c": _downsample(temp),
            "target_pressure_bar": _downsample(dp.get("targetPressure") or []),
            "target_flow_mls": _downsample(dp.get("targetPumpFlow") or []),
        },
        # The profile phases the shot ran (targets/stop conditions), verbatim —
        # useful context for diagnosing whether the machine tracked its plan.
        "profile_phases": profile.get("phases"),
    }
    if temps and target_temps:
        transformed["avg_temp_deviation_c"] = round(
            sum(t - tt for t, tt in zip(temps, target_temps)) / min(len(temps), len(target_temps)),
            2,
        )
    return transformed


def _extract_latest_id(payload: Any) -> Optional[int]:
    """Pull the newest shot id out of /api/shots/latest (list- or dict-shaped)."""
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        for key in ("lastShotId", "id", "shotId"):
            if key in payload:
                try:
                    return int(payload[key])
                except (TypeError, ValueError):
                    return None
    if isinstance(payload, (int, str)):
        try:
            return int(payload)
        except ValueError:
            return None
    return None


async def _get_json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def ingest_new_shots_gaggiuino(
    conn: aiosqlite.Connection, config: CremaConfig, limit: int = 25
) -> list[str]:
    """Fetch up to `limit` recent Gaggiuino shots not already in the DB.

    Returns newly ingested (zero-padded) shot ids, newest first — the same
    contract as the GaggiMate ingest.
    """
    await db.prune_old(conn, config.retention_days)
    base = config.gaggiuino_url.rstrip("/")
    known = await db.known_shot_ids(conn)
    active = await db.active_bean(conn)
    bean_id = active["id"] if active else None
    coffee = (
        db.canonical_coffee(active)
        if active
        else (await db.get_setting(conn, "coffee")) or config.coffee or None
    )

    new_ids: list[str] = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        latest = _extract_latest_id(await _get_json(session, f"{base}/api/shots/latest"))
        if latest is None:
            return []
        for shot_id in range(latest, max(0, latest - limit), -1):
            padded = str(shot_id).zfill(6)
            if padded in known:
                continue
            try:
                shot = await _get_json(session, f"{base}/api/shots/{shot_id}")
            except aiohttp.ClientResponseError:
                continue  # gaps happen (deleted shots); keep walking the range
            if not isinstance(shot, dict) or not shot.get("datapoints"):
                continue
            transformed = transform_gaggiuino_shot(shot)
            await db.upsert_shot(
                conn,
                shot_id=transformed["shot_id"],
                transformed=transformed,
                captured_at=float(shot["timestamp"]) if shot.get("timestamp") else None,
                coffee=coffee,
                bean_id=bean_id,
            )
            new_ids.append(transformed["shot_id"])
    return new_ids
