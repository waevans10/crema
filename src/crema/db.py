"""SQLite persistence for crema (async, via aiosqlite).

Phase 1 stores two things:
  * shots    — one row per shot pulled from the device, holding the AI-friendly
               JSON produced by gaggimate_mcp's transformer.
  * reviews  — one row per Claude review, linked to the newest shot it covered.

Later phases add `pending_edits` (drafted profile changes awaiting approval) and
`profiles`; the schema is intentionally easy to extend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    id            TEXT PRIMARY KEY,       -- device shot id (zero-padded, e.g. "000123")
    captured_at   REAL,                   -- unix seconds if known, else NULL
    transformed   TEXT NOT NULL,          -- AI-friendly JSON (transform_shot_for_ai output)
    created_at    REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id       TEXT NOT NULL,          -- newest shot this review covered
    model         TEXT NOT NULL,
    suggestions   TEXT NOT NULL,          -- structured JSON from Claude
    created_at    REAL NOT NULL DEFAULT (unixepoch('now')),
    FOREIGN KEY (shot_id) REFERENCES shots(id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_shot ON reviews(shot_id);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews(created_at DESC);
"""


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open the DB, apply pragmas, and ensure the schema exists."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.executescript(SCHEMA)
    await db.commit()
    return db


async def known_shot_ids(db: aiosqlite.Connection) -> set[str]:
    """Return the set of shot ids already stored (for incremental ingest)."""
    async with db.execute("SELECT id FROM shots") as cur:
        return {row["id"] async for row in cur}


async def upsert_shot(
    db: aiosqlite.Connection,
    shot_id: str,
    transformed: dict[str, Any],
    captured_at: Optional[float] = None,
) -> None:
    """Insert or replace a shot's AI-friendly JSON."""
    await db.execute(
        "INSERT INTO shots (id, captured_at, transformed) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET transformed=excluded.transformed, "
        "captured_at=excluded.captured_at",
        (shot_id, captured_at, json.dumps(transformed)),
    )
    await db.commit()


async def recent_shots(db: aiosqlite.Connection, limit: int) -> list[dict[str, Any]]:
    """Return the `limit` most recent shots, newest first, as parsed dicts.

    Ordered by captured_at when present, falling back to insertion order.
    """
    async with db.execute(
        "SELECT id, captured_at, transformed FROM shots "
        "ORDER BY COALESCE(captured_at, created_at) DESC, rowid DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"id": r["id"], "captured_at": r["captured_at"], "transformed": json.loads(r["transformed"])}
        for r in rows
    ]


async def insert_review(
    db: aiosqlite.Connection, shot_id: str, model: str, suggestions: dict[str, Any]
) -> int:
    """Store a review and return its row id."""
    cur = await db.execute(
        "INSERT INTO reviews (shot_id, model, suggestions) VALUES (?, ?, ?)",
        (shot_id, model, json.dumps(suggestions)),
    )
    await db.commit()
    return int(cur.lastrowid)


async def latest_review(db: aiosqlite.Connection) -> Optional[dict[str, Any]]:
    """Return the most recent review, or None."""
    async with db.execute(
        "SELECT id, shot_id, model, suggestions, created_at FROM reviews "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "shot_id": row["shot_id"],
        "model": row["model"],
        "suggestions": json.loads(row["suggestions"]),
        "created_at": row["created_at"],
    }
