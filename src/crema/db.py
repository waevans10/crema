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

CREATE TABLE IF NOT EXISTS pending_edits (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id          INTEGER,                -- source review, if any
    base_profile_id    TEXT,                   -- device profile the draft is based on
    base_profile_label TEXT,
    label              TEXT NOT NULL,          -- proposed label (AI suffix added on push)
    change_summary     TEXT NOT NULL,
    profile_json       TEXT NOT NULL,          -- validated, push-ready device profile
    status             TEXT NOT NULL DEFAULT 'draft',  -- draft|pushed|discarded|failed
    device_profile_id  TEXT,                   -- id returned by the device after push
    error              TEXT,
    created_at         REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at         REAL NOT NULL DEFAULT (unixepoch('now')),
    FOREIGN KEY (review_id) REFERENCES reviews(id)
);

CREATE INDEX IF NOT EXISTS idx_edits_status ON pending_edits(status, created_at DESC);
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


async def get_shot(db: aiosqlite.Connection, shot_id: str) -> Optional[dict[str, Any]]:
    """Return a single shot's stored data by id, or None."""
    async with db.execute(
        "SELECT id, captured_at, transformed FROM shots WHERE id = ?", (shot_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "captured_at": row["captured_at"], "transformed": json.loads(row["transformed"])}


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


def _review_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "shot_id": row["shot_id"],
        "model": row["model"],
        "suggestions": json.loads(row["suggestions"]),
        "created_at": row["created_at"],
    }


async def latest_review(db: aiosqlite.Connection) -> Optional[dict[str, Any]]:
    """Return the most recent review, or None."""
    async with db.execute(
        "SELECT id, shot_id, model, suggestions, created_at FROM reviews "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    return _review_row(row) if row else None


async def get_review(db: aiosqlite.Connection, review_id: int) -> Optional[dict[str, Any]]:
    """Return a single review by id, or None."""
    async with db.execute(
        "SELECT id, shot_id, model, suggestions, created_at FROM reviews WHERE id = ?",
        (review_id,),
    ) as cur:
        row = await cur.fetchone()
    return _review_row(row) if row else None


# --- pending profile edits (Phase 2) ---


async def insert_pending_edit(
    db: aiosqlite.Connection,
    label: str,
    change_summary: str,
    profile: dict[str, Any],
    review_id: Optional[int] = None,
    base_profile_id: Optional[str] = None,
    base_profile_label: Optional[str] = None,
) -> int:
    """Store a drafted profile edit (status 'draft') and return its id."""
    cur = await db.execute(
        "INSERT INTO pending_edits "
        "(review_id, base_profile_id, base_profile_label, label, change_summary, profile_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (review_id, base_profile_id, base_profile_label, label, change_summary, json.dumps(profile)),
    )
    await db.commit()
    return int(cur.lastrowid)


def _edit_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "review_id": row["review_id"],
        "base_profile_id": row["base_profile_id"],
        "base_profile_label": row["base_profile_label"],
        "label": row["label"],
        "change_summary": row["change_summary"],
        "profile": json.loads(row["profile_json"]),
        "status": row["status"],
        "device_profile_id": row["device_profile_id"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


async def get_pending_edit(db: aiosqlite.Connection, edit_id: int) -> Optional[dict[str, Any]]:
    """Return a pending edit by id, or None."""
    async with db.execute("SELECT * FROM pending_edits WHERE id = ?", (edit_id,)) as cur:
        row = await cur.fetchone()
    return _edit_row(row) if row else None


async def list_pending_edits(
    db: aiosqlite.Connection, status: Optional[str] = None, limit: int = 20
) -> list[dict[str, Any]]:
    """List edits, optionally filtered by status, newest first."""
    if status:
        query = "SELECT * FROM pending_edits WHERE status = ? ORDER BY created_at DESC LIMIT ?"
        args: tuple[Any, ...] = (status, limit)
    else:
        query = "SELECT * FROM pending_edits ORDER BY created_at DESC LIMIT ?"
        args = (limit,)
    async with db.execute(query, args) as cur:
        rows = await cur.fetchall()
    return [_edit_row(r) for r in rows]


async def set_edit_status(
    db: aiosqlite.Connection,
    edit_id: int,
    status: str,
    device_profile_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update an edit's status (and push result / error)."""
    await db.execute(
        "UPDATE pending_edits SET status = ?, device_profile_id = ?, error = ?, "
        "updated_at = unixepoch('now') WHERE id = ?",
        (status, device_profile_id, error, edit_id),
    )
    await db.commit()
