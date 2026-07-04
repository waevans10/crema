"""Anonymized shot-data export, and opt-in sharing to the community pool.

The bundle is the dataset format: shots in chronological order with their
telemetry, beans, and tasting notes, plus every review's advice — enough to
reconstruct (context → advice → next shot → outcome) training examples.

Identity is a random install UUID generated once and stored in settings.
Nothing else identifying is added — but free-text fields (profile labels,
coffee, tasting notes, grinder) are whatever the barista typed, so `crema
export` exists precisely so you can read what would be shared before sharing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiohttp
import aiosqlite

from . import db
from .config import CremaConfig

SCHEMA_VERSION = 1

# Bump when SHARE_TERMS changes materially. Stamped into every shared bundle
# (with the acceptance time) so each stored submission carries its own
# evidence of which terms were accepted.
TERMS_VERSION = 1

# What opting in means. Shown by `crema share` and must be accepted explicitly.
SHARE_TERMS = """\
Sharing sends this export bundle to the crema community shot pool:
  * your shots' telemetry (pressure/flow/temperature diagnostics)
  * profile names, grinder, coffee, and tasting notes AS YOU TYPED THEM
  * the AI reviews' advice, so advice->outcome pairs can be studied
  * a random install id (no name, email, or network details)

By sharing you grant the crema project a perpetual, worldwide, royalty-free
license to use, modify, and redistribute this data, INCLUDING COMMERCIALLY.
The pooled dataset is published for community use under CC BY-NC 4.0
(non-commercial, attribution).

Withdrawal: you can request deletion of your submitted bundles at any time by
opening a GitHub issue with your install id (shown by `crema export`). Raw
submissions are then removed from the pool; data already included in a
published dataset release stays licensed as released.

Run `crema export` first if you want to read exactly what leaves your box.\
"""


def stamp_acceptance(bundle: dict[str, Any]) -> dict[str, Any]:
    """Record which terms were accepted, and when, inside the bundle itself."""
    import datetime as _dt

    bundle["terms_version"] = TERMS_VERSION
    bundle["terms_accepted_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return bundle


async def get_install_id(conn: aiosqlite.Connection) -> str:
    """Return this install's random UUID, creating it on first use."""
    existing = await db.get_setting(conn, "install_id")
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    await db.set_setting(conn, "install_id", new_id)
    return new_id


async def build_export_bundle(conn: aiosqlite.Connection, config: CremaConfig) -> dict[str, Any]:
    """Assemble the anonymized export bundle from everything in the DB."""
    install_id = await get_install_id(conn)
    grinder = (await db.get_setting(conn, "grinder")) or config.grinder or None

    # Chronological (oldest first) so advice->next-shot pairs read in order.
    shots = list(reversed(await db.recent_shots(conn, limit=100_000)))
    async with conn.execute(
        "SELECT shot_id, model, suggestions, created_at FROM reviews ORDER BY created_at ASC, id ASC"
    ) as cur:
        review_rows = await cur.fetchall()

    return {
        "schema_version": SCHEMA_VERSION,
        "install_id": install_id,
        # Which machine platform produced the telemetry. Only GaggiMate today;
        # recorded per bundle so future adapters pool cleanly alongside it.
        "machine": "gaggimate",
        "grinder": grinder,
        "shots": [
            {
                "id": s["id"],
                "captured_at": s["captured_at"],
                "coffee": s.get("coffee"),
                "tasting_notes": s.get("tasting_notes"),
                "telemetry": s["transformed"],
            }
            for s in shots
        ],
        "reviews": [
            {
                "shot_id": r["shot_id"],
                "model": r["model"],
                "suggestions": json.loads(r["suggestions"]),
                "created_at": r["created_at"],
            }
            for r in review_rows
        ],
    }


async def share_bundle(bundle: dict[str, Any], share_url: str) -> str:
    """POST the bundle to the community pool endpoint. Returns the server's reply."""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            share_url,
            json=bundle,
            headers={"User-Agent": "crema-share/1"},
        ) as resp:
            text = (await resp.text())[:500]
            if resp.status != 200:
                raise RuntimeError(f"Share failed (HTTP {resp.status}): {text}")
            return text
