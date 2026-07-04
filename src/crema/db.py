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
    tasting_notes TEXT,                   -- barista's taste feedback, fed into future reviews
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
    notes              TEXT,                   -- barista notes given to Claude for this draft
    stop_changes       TEXT,                   -- JSON list: stop-condition changes vs base (must be acknowledged)
    created_at         REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at         REAL NOT NULL DEFAULT (unixepoch('now')),
    FOREIGN KEY (review_id) REFERENCES reviews(id)
);

CREATE INDEX IF NOT EXISTS idx_edits_status ON pending_edits(status, created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Cached device profiles, captured during ingest so drafting works without the
-- machine online (the machine is only needed to push an approved edit).
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,       -- device profile id
    label       TEXT,
    data        TEXT NOT NULL,          -- full profile JSON
    updated_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);
"""


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open the DB, apply pragmas, and ensure the schema exists."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.executescript(SCHEMA)
    await _migrate(db)
    await db.commit()
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Additive column migrations for DBs created before a column existed."""
    async with db.execute("PRAGMA table_info(pending_edits)") as cur:
        cols = {row["name"] async for row in cur}
    if "notes" not in cols:
        # Barista notes given to Claude when drafting/refining this edit.
        await db.execute("ALTER TABLE pending_edits ADD COLUMN notes TEXT")
    if "stop_changes" not in cols:
        # JSON list of human-readable stop-condition changes vs the base profile;
        # non-empty means the user must explicitly acknowledge before pushing.
        await db.execute("ALTER TABLE pending_edits ADD COLUMN stop_changes TEXT")
    async with db.execute("PRAGMA table_info(shots)") as cur:
        shot_cols = {row["name"] async for row in cur}
    if "tasting_notes" not in shot_cols:
        # Barista's taste feedback on a shot, included in future review context.
        await db.execute("ALTER TABLE shots ADD COLUMN tasting_notes TEXT")


async def upsert_profile(
    db: aiosqlite.Connection, profile_id: str, label: Optional[str], data: dict[str, Any]
) -> None:
    """Cache a device profile's full JSON for offline drafting."""
    await db.execute(
        "INSERT INTO profiles (id, label, data, updated_at) VALUES (?, ?, ?, unixepoch('now')) "
        "ON CONFLICT(id) DO UPDATE SET label=excluded.label, data=excluded.data, "
        "updated_at=excluded.updated_at",
        (profile_id, label, json.dumps(data)),
    )
    await db.commit()


async def get_profile(db: aiosqlite.Connection, profile_id: str) -> Optional[dict[str, Any]]:
    """Return a cached profile's full JSON, or None if not cached."""
    async with db.execute("SELECT data FROM profiles WHERE id = ?", (profile_id,)) as cur:
        row = await cur.fetchone()
    return json.loads(row["data"]) if row else None


async def get_setting(db: aiosqlite.Connection, key: str) -> Optional[str]:
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def get_bool_setting(db: aiosqlite.Connection, key: str, default: bool) -> bool:
    """Read a boolean setting, falling back to `default` when it's never been set."""
    raw = await get_setting(db, key)
    return default if raw is None else raw == "1"


async def prune_old(db: aiosqlite.Connection, retention_days: int) -> int:
    """Delete shots (and their reviews/edits) ingested more than N days ago.

    Uses crema's own insertion time (created_at), not the device timestamp, so it's
    robust regardless of the machine's clock. Deletes children first to satisfy
    foreign keys. Returns the number of shots removed. No-op when retention_days<=0.
    """
    if retention_days <= 0:
        return 0
    cutoff = f"unixepoch('now') - {int(retention_days) * 86400}"
    old = f"(SELECT id FROM shots WHERE created_at < {cutoff})"
    await db.execute(
        f"DELETE FROM pending_edits WHERE review_id IN "
        f"(SELECT id FROM reviews WHERE shot_id IN {old})"
    )
    await db.execute(f"DELETE FROM reviews WHERE shot_id IN {old}")
    cur = await db.execute(f"DELETE FROM shots WHERE created_at < {cutoff}")
    await db.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


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
        "SELECT id, captured_at, transformed, tasting_notes FROM shots "
        "ORDER BY COALESCE(captured_at, created_at) DESC, rowid DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [_shot_row(r) for r in rows]


def _shot_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "captured_at": row["captured_at"],
        "transformed": json.loads(row["transformed"]),
        "tasting_notes": row["tasting_notes"],
    }


async def get_shot(db: aiosqlite.Connection, shot_id: str) -> Optional[dict[str, Any]]:
    """Return a single shot's stored data by id, or None."""
    async with db.execute(
        "SELECT id, captured_at, transformed, tasting_notes FROM shots WHERE id = ?", (shot_id,)
    ) as cur:
        row = await cur.fetchone()
    return _shot_row(row) if row else None


async def set_shot_tasting_notes(
    db: aiosqlite.Connection, shot_id: str, notes: Optional[str]
) -> bool:
    """Save (or clear, with None/empty) the barista's tasting notes for a shot.

    Returns False when the shot id doesn't exist.
    """
    cur = await db.execute(
        "UPDATE shots SET tasting_notes = ? WHERE id = ?",
        (notes or None, shot_id),
    )
    await db.commit()
    return bool(cur.rowcount)


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


async def latest_reviews_for_shots(
    db: aiosqlite.Connection, shot_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Map shot id → the latest review's suggestions for each shot that has one.

    Used to interleave past advice with the shot history in review context, so
    Claude can see whether its earlier recommendations worked.
    """
    if not shot_ids:
        return {}
    placeholders = ",".join("?" for _ in shot_ids)
    async with db.execute(
        f"SELECT shot_id, suggestions FROM reviews WHERE shot_id IN ({placeholders}) "
        "ORDER BY created_at ASC, id ASC",  # later rows overwrite → latest wins
        tuple(shot_ids),
    ) as cur:
        rows = await cur.fetchall()
    return {r["shot_id"]: json.loads(r["suggestions"]) for r in rows}


async def get_review(db: aiosqlite.Connection, review_id: int) -> Optional[dict[str, Any]]:
    """Return a single review by id, or None."""
    async with db.execute(
        "SELECT id, shot_id, model, suggestions, created_at FROM reviews WHERE id = ?",
        (review_id,),
    ) as cur:
        row = await cur.fetchone()
    return _review_row(row) if row else None


# --- pending profile edits (drafted, awaiting approval) ---


async def insert_pending_edit(
    db: aiosqlite.Connection,
    label: str,
    change_summary: str,
    profile: dict[str, Any],
    review_id: Optional[int] = None,
    base_profile_id: Optional[str] = None,
    base_profile_label: Optional[str] = None,
    notes: Optional[str] = None,
    stop_changes: Optional[list[str]] = None,
) -> int:
    """Store a drafted profile edit (status 'draft') and return its id."""
    cur = await db.execute(
        "INSERT INTO pending_edits "
        "(review_id, base_profile_id, base_profile_label, label, change_summary, profile_json, "
        " notes, stop_changes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            review_id,
            base_profile_id,
            base_profile_label,
            label,
            change_summary,
            json.dumps(profile),
            notes,
            json.dumps(stop_changes) if stop_changes else None,
        ),
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
        "notes": row["notes"],
        "stop_changes": json.loads(row["stop_changes"]) if row["stop_changes"] else [],
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
