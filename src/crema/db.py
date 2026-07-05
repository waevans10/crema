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
    coffee        TEXT,                   -- beans this shot was pulled with (stamped at ingest, editable)
    created_at    REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id       TEXT NOT NULL,          -- newest shot this review covered
    model         TEXT NOT NULL,
    suggestions   TEXT NOT NULL,          -- structured JSON from Claude
    input_tokens  INTEGER,                -- Claude usage for this review (cost audit)
    output_tokens INTEGER,
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

-- The barista's bean library. Structured (restricted roast level) so a new bag
-- can be matched against similar past beans for a starting point, and so roast
-- date can drive an "aging" warning. Freetext `coffee` on shots is still stamped
-- for backward compatibility and display; a bean just gives it structure.
CREATE TABLE IF NOT EXISTS beans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,          -- e.g. "Colombian Huila"
    -- Restricted vocabulary, enforced at the DB layer too (not just in Python)
    -- so a stray write can't store an unmatchable roast level.
    roast_level  TEXT NOT NULL CHECK (roast_level IN
                   ('light','medium-light','medium','medium-dark','dark')),
    process      TEXT,                   -- optional: washed|natural|honey|anaerobic|other
    roast_date   TEXT,                   -- optional ISO date (YYYY-MM-DD)
    notes        TEXT,
    created_at   REAL NOT NULL DEFAULT (unixepoch('now'))
);
"""

# The roast levels the UI/CLI accept — a fixed vocabulary keeps bean data unified
# so similarity matching is reliable. Ordered light → dark (index = adjacency).
ROAST_LEVELS = ["light", "medium-light", "medium", "medium-dark", "dark"]
# Optional processing methods, likewise restricted.
PROCESSES = ["washed", "natural", "honey", "anaerobic", "other"]


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
    if "coffee" not in shot_cols:
        # Beans the shot was pulled with — stamped from the coffee setting at
        # ingest, editable per shot, so a bean change mid-window stays accurate.
        await db.execute("ALTER TABLE shots ADD COLUMN coffee TEXT")
    if "bean_id" not in shot_cols:
        # Link to a structured bean (beans table), when one is active at ingest.
        # Nullable — legacy shots and freetext-only setups just leave it NULL.
        await db.execute("ALTER TABLE shots ADD COLUMN bean_id INTEGER")
    async with db.execute("PRAGMA table_info(reviews)") as cur:
        review_cols = {row["name"] async for row in cur}
    if "input_tokens" not in review_cols:
        # Claude token usage per review, persisted for after-the-fact cost audit.
        await db.execute("ALTER TABLE reviews ADD COLUMN input_tokens INTEGER")
        await db.execute("ALTER TABLE reviews ADD COLUMN output_tokens INTEGER")


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
    # Parameterized (not interpolated) so the query stays injection-proof even if
    # the int() guard is ever refactored away.
    cutoff_secs = int(retention_days) * 86400
    old = "(SELECT id FROM shots WHERE created_at < unixepoch('now') - ?)"
    await db.execute(
        f"DELETE FROM pending_edits WHERE review_id IN "
        f"(SELECT id FROM reviews WHERE shot_id IN {old})",
        (cutoff_secs,),
    )
    await db.execute(f"DELETE FROM reviews WHERE shot_id IN {old}", (cutoff_secs,))
    cur = await db.execute(
        "DELETE FROM shots WHERE created_at < unixepoch('now') - ?", (cutoff_secs,)
    )
    await db.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


async def known_shot_ids(db: aiosqlite.Connection) -> set[str]:
    """Return the set of shot ids already stored (for incremental ingest).

    Loads all ids into memory, which is fine because `prune_old` bounds the shots
    table to the retention window (~weeks of shots). If retention is ever removed,
    switch this to a per-shot existence check.
    """
    async with db.execute("SELECT id FROM shots") as cur:
        return {row["id"] async for row in cur}


async def upsert_shot(
    db: aiosqlite.Connection,
    shot_id: str,
    transformed: dict[str, Any],
    captured_at: Optional[float] = None,
    coffee: Optional[str] = None,
    bean_id: Optional[int] = None,
) -> None:
    """Insert or replace a shot's AI-friendly JSON.

    `coffee` (the beans at ingest time) and `bean_id` (the structured bean, when
    one is active) are only set on first insert — a re-ingest never overwrites a
    coffee/bean the barista may have edited.
    """
    await db.execute(
        "INSERT INTO shots (id, captured_at, transformed, coffee, bean_id) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET transformed=excluded.transformed, "
        "captured_at=excluded.captured_at",
        (shot_id, captured_at, json.dumps(transformed), coffee or None, bean_id),
    )
    await db.commit()


async def recent_shots(db: aiosqlite.Connection, limit: int) -> list[dict[str, Any]]:
    """Return the `limit` most recent shots, newest first, as parsed dicts.

    Ordered by captured_at when present, falling back to insertion order.
    """
    async with db.execute(
        "SELECT id, captured_at, transformed, tasting_notes, coffee, bean_id FROM shots "
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
        "coffee": row["coffee"],
        "bean_id": row["bean_id"],
    }


async def get_shot(db: aiosqlite.Connection, shot_id: str) -> Optional[dict[str, Any]]:
    """Return a single shot's stored data by id, or None."""
    async with db.execute(
        "SELECT id, captured_at, transformed, tasting_notes, coffee, bean_id FROM shots WHERE id = ?",
        (shot_id,),
    ) as cur:
        row = await cur.fetchone()
    return _shot_row(row) if row else None


async def set_shot_coffee(db: aiosqlite.Connection, shot_id: str, coffee: Optional[str]) -> bool:
    """Save (or clear, with None/empty) which beans a shot was pulled with.

    Returns False when the shot id doesn't exist.
    """
    cur = await db.execute(
        "UPDATE shots SET coffee = ? WHERE id = ?",
        (coffee or None, shot_id),
    )
    await db.commit()
    return bool(cur.rowcount)


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
    db: aiosqlite.Connection,
    shot_id: str,
    model: str,
    suggestions: dict[str, Any],
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> int:
    """Store a review (with its Claude token usage) and return its row id."""
    cur = await db.execute(
        "INSERT INTO reviews (shot_id, model, suggestions, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?)",
        (shot_id, model, json.dumps(suggestions), input_tokens, output_tokens),
    )
    await db.commit()
    return int(cur.lastrowid)


def _review_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "shot_id": row["shot_id"],
        "model": row["model"],
        "suggestions": json.loads(row["suggestions"]),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "created_at": row["created_at"],
    }


async def latest_review(db: aiosqlite.Connection) -> Optional[dict[str, Any]]:
    """Return the most recent review, or None."""
    async with db.execute(
        "SELECT id, shot_id, model, suggestions, input_tokens, output_tokens, created_at "
        "FROM reviews ORDER BY created_at DESC, id DESC LIMIT 1"
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
        "SELECT id, shot_id, model, suggestions, input_tokens, output_tokens, created_at "
        "FROM reviews WHERE id = ?",
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


# --- beans (the structured bean library) ---


def canonical_coffee(bean: dict[str, Any]) -> str:
    """The freetext `coffee` string stamped onto shots for a structured bean.

    Keeps the review prompt (which reads `coffee`) working unchanged, and gives a
    consistent human label. e.g. "Colombian Huila · light roast · washed · roasted 2026-06-20".
    """
    parts = [str(bean["name"]).strip(), f"{bean['roast_level']} roast"]
    if bean.get("process"):
        parts.append(str(bean["process"]))
    if bean.get("roast_date"):
        parts.append(f"roasted {bean['roast_date']}")
    return " · ".join(p for p in parts if p)


def _bean_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "roast_level": row["roast_level"],
        "process": row["process"],
        "roast_date": row["roast_date"],
        "notes": row["notes"],
        "created_at": row["created_at"],
    }


async def insert_bean(
    db: aiosqlite.Connection,
    name: str,
    roast_level: str,
    process: Optional[str] = None,
    roast_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Add a bean to the library and return its id."""
    cur = await db.execute(
        "INSERT INTO beans (name, roast_level, process, roast_date, notes) VALUES (?, ?, ?, ?, ?)",
        (name, roast_level, process or None, roast_date or None, notes or None),
    )
    await db.commit()
    return int(cur.lastrowid)


async def get_bean(db: aiosqlite.Connection, bean_id: int) -> Optional[dict[str, Any]]:
    """Return a bean by id, or None."""
    async with db.execute("SELECT * FROM beans WHERE id = ?", (bean_id,)) as cur:
        row = await cur.fetchone()
    return _bean_row(row) if row else None


async def list_beans(db: aiosqlite.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """List beans, newest first."""
    async with db.execute(
        "SELECT * FROM beans ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return [_bean_row(r) for r in rows]


async def active_bean(db: aiosqlite.Connection) -> Optional[dict[str, Any]]:
    """The bean currently in the hopper (setting `active_bean_id`), or None."""
    raw = await get_setting(db, "active_bean_id")
    if not raw:
        return None
    try:
        return await get_bean(db, int(raw))
    except (TypeError, ValueError):
        return None


async def set_active_bean(db: aiosqlite.Connection, bean_id: Optional[int]) -> None:
    """Set (or clear, with None) which bean is active; also stamps the coffee setting."""
    if bean_id is None:
        await set_setting(db, "active_bean_id", "")
        return
    bean = await get_bean(db, bean_id)
    if bean is None:
        raise ValueError(f"Bean {bean_id} not found.")
    await set_setting(db, "active_bean_id", str(bean_id))
    # Keep the freetext coffee setting in sync so reviews stay grounded.
    await set_setting(db, "coffee", canonical_coffee(bean))


async def score_history(db: aiosqlite.Connection, limit: int = 40) -> list[dict[str, Any]]:
    """Oldest→newest shot rows with their latest review score, for the trend view.

    Only shots that have been reviewed carry a score; others come through with
    score=None so dose/yield are still plottable in the table.
    """
    async with db.execute(
        "SELECT id, captured_at, transformed, coffee FROM shots "
        "ORDER BY COALESCE(captured_at, created_at) DESC, rowid DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    shot_ids = [r["id"] for r in rows]
    reviews = await latest_reviews_for_shots(db, shot_ids)
    out: list[dict[str, Any]] = []
    for r in rows:
        t = json.loads(r["transformed"])
        sugg = reviews.get(r["id"]) or {}
        score = sugg.get("score")
        out.append(
            {
                "id": r["id"],
                "captured_at": r["captured_at"],
                "coffee": r["coffee"],
                "score": score if isinstance(score, int) else None,
                # Dose isn't in the telemetry (the machine doesn't know it), so the
                # trend works with what the shot actually reports: yield + duration.
                "yield_g": t.get("final_weight_g"),
                "duration_s": t.get("duration_seconds"),
            }
        )
    out.reverse()  # oldest → newest for left-to-right plotting
    return out
