"""Pull new shots from the GaggiMate device and store their AI-friendly JSON.

Reuses the vendored gaggimate_mcp HTTP client (binary .slog download) and
transformer (physics-informed diagnostics). Only shots not already in the DB are
fetched, so this is cheap to run repeatedly / on a schedule.
"""

from __future__ import annotations

import aiosqlite

from gaggimate_mcp.api.http import GaggimateHTTPClient
from gaggimate_mcp.transformers.shot import transform_shot_for_ai

from . import db
from .config import CremaConfig

# Detail level fed to Claude. "per_phase" includes per-phase channeling/resistance
# diagnostics without per-sample curves — a good accuracy/token balance for review.
REVIEW_DETAIL = "per_phase"


async def ingest_new_shots(conn: aiosqlite.Connection, config: CremaConfig, limit: int = 25) -> list[str]:
    """Fetch up to `limit` recent shots, store any not already known.

    Returns the list of newly ingested (padded) shot ids, newest first.
    """
    client = GaggimateHTTPClient(config.gaggimate())
    index = await client.list_recent_shots(limit=limit)
    known = await db.known_shot_ids(conn)

    new_ids: list[str] = []
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
        )
        new_ids.append(transformed["shot_id"])
    return new_ids
