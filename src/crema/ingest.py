"""Pull new shots from the GaggiMate device and store their AI-friendly JSON.

Reuses the vendored gaggimate_mcp HTTP client (binary .slog download) and
transformer (physics-informed diagnostics). Only shots not already in the DB are
fetched, so this is cheap to run repeatedly / on a schedule.
"""

from __future__ import annotations

import logging

import aiosqlite

from gaggimate_mcp.api.http import GaggimateHTTPClient
from gaggimate_mcp.api.websocket import GaggimateWebSocketClient
from gaggimate_mcp.transformers.shot import transform_shot_for_ai

from . import db
from .config import CremaConfig

_log = logging.getLogger(__name__)

# Detail level fed to Claude. "per_phase" includes per-phase channeling/resistance
# diagnostics without per-sample curves — a good accuracy/token balance for review.
REVIEW_DETAIL = "per_phase"


async def ingest_new_shots(conn: aiosqlite.Connection, config: CremaConfig, limit: int = 25) -> list[str]:
    """Fetch up to `limit` recent shots, store any not already known.

    Returns the list of newly ingested (padded) shot ids, newest first.
    Dispatches to the machine adapter selected by CREMA_MACHINE.
    """
    if config.machine == "gaggiuino":
        from .gaggiuino import ingest_new_shots_gaggiuino

        return await ingest_new_shots_gaggiuino(conn, config, limit=limit)

    # Keep the DB bounded: drop shots older than the retention window.
    await db.prune_old(conn, config.retention_days)

    client = GaggimateHTTPClient(config.gaggimate())
    index = await client.list_recent_shots(limit=limit)
    known = await db.known_shot_ids(conn)

    # Stamp new shots with the beans currently in the hopper, so a bean change
    # later doesn't rewrite these shots' history. Prefer the structured active
    # bean (also linked by id, for similarity matching); fall back to the
    # freetext coffee setting for setups that don't use the bean library.
    active = await db.active_bean(conn)
    bean_id = active["id"] if active else None
    coffee = (
        db.canonical_coffee(active)
        if active
        else (await db.get_setting(conn, "coffee")) or config.coffee or None
    )

    new_ids: list[str] = []
    profile_ids: set[str] = set()
    for meta in index:
        padded = str(meta["id"]).zfill(6)
        if padded in known:
            continue
        shot = await client.fetch_shot(padded)
        if shot is None:
            continue
        transformed = transform_shot_for_ai(shot, detail=REVIEW_DETAIL)
        captured_at = transformed.get("timestamp") or meta.get("timestamp")
        await db.upsert_shot(
            conn,
            shot_id=transformed["shot_id"],
            transformed=dict(transformed),
            captured_at=float(captured_at) if captured_at is not None else None,
            coffee=coffee,
            bean_id=bean_id,
        )
        await db.assign_shot_to_active_experiment(conn, transformed["shot_id"], bean_id)
        new_ids.append(transformed["shot_id"])
        pid = transformed.get("profile_id")
        if pid:
            profile_ids.add(str(pid))

    # Cache the profiles used by new + recent shots, while the machine is
    # reachable, so drafting an edit later doesn't need the machine on. Only
    # fetches profiles we haven't cached yet. Best-effort — never fails ingest.
    for sh in await db.recent_shots(conn, limit=config.review_window):
        pid = sh["transformed"].get("profile_id")
        if pid:
            profile_ids.add(str(pid))
    ws = GaggimateWebSocketClient(config.gaggimate())
    for pid in profile_ids:
        if await db.get_profile(conn, pid) is not None:
            continue
        try:
            profile = await ws.load_profile(pid)
            if profile:
                await db.upsert_profile(conn, pid, profile.get("label"), profile)
        except Exception as e:  # noqa: BLE001 — caching is best-effort, never fails ingest
            # Logged at debug so "why isn't this profile cached for drafting?" is
            # diagnosable, without noising up a normal run.
            _log.debug("Profile %s failed to cache: %s", pid, e)

    return new_ids
