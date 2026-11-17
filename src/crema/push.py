"""Approve/push and discard drafted profile edits.

Pushing creates a NEW profile on the machine (the vendored client appends the
" [AI]" label suffix), so the user's original profile is never overwritten. The
new profile is saved but not auto-selected — you pick it on the machine.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from gaggimate_mcp.api.websocket import GaggimateWebSocketClient

from . import db
from .config import CremaConfig


async def push_edit(
    conn: aiosqlite.Connection, config: CremaConfig, edit_id: int, *,
    acknowledge_stops: bool = False,
) -> dict[str, Any]:
    """Push an approved edit to the machine as a new profile.

    Returns the updated edit dict. Raises if the edit isn't in 'draft' state.
    """
    edit = await db.get_pending_edit(conn, edit_id)
    if edit is None:
        raise RuntimeError(f"Edit {edit_id} not found.")
    if edit["status"] != "draft":
        raise RuntimeError(f"Edit {edit_id} is '{edit['status']}', not a draft — nothing to push.")

    if config.machine != "gaggimate":
        raise RuntimeError("Profile push is GaggiMate-only.")
    if edit["review_id"] is not None:
        await db.require_rated_review(conn, edit["review_id"])
    if edit["stop_changes"] and not acknowledge_stops:
        raise RuntimeError(
            "Review and acknowledge the changed stop conditions before pushing "
            "(CLI: --acknowledge-stops). Changes: " + "; ".join(edit["stop_changes"])
        )

    profile = edit["profile"]
    ws = GaggimateWebSocketClient(config.gaggimate())
    claim = await conn.execute(
        "UPDATE pending_edits SET status='pushing', updated_at=unixepoch('now') "
        "WHERE id=? AND status='draft'", (edit_id,),
    )
    await conn.commit()
    if claim.rowcount != 1:
        raise RuntimeError("Draft was already claimed or changed; nothing to push.")
    # A crash after the claim leaves 'pushing': never automatically repeat a device write.
    try:
        saved = await ws.create_or_update_profile(
            label=profile["label"],
            temperature=profile["temperature"],
            phases=profile["phases"],
            profile_id=None,  # always create new — never overwrite the original
            profile_type=profile.get("type", "pro"),
        )
        device_id = str(saved["id"]) if saved.get("id") is not None else None
        if not device_id:
            raise RuntimeError("Device returned no saved profile ID.")
    except Exception as e:
        message = f"Push outcome unknown. Check the machine before creating another draft: {e}"
        await db.set_edit_status(conn, edit_id, "unknown", error=message)
        raise RuntimeError(message) from e

    await db.set_edit_status(conn, edit_id, "pushed", device_profile_id=device_id)
    result = await db.get_pending_edit(conn, edit_id)
    assert result is not None
    return result


async def discard_edit(conn: aiosqlite.Connection, edit_id: int) -> None:
    """Mark a drafted edit as discarded."""
    changed = await conn.execute(
        "UPDATE pending_edits SET status='discarded', updated_at=unixepoch('now') "
        "WHERE id=? AND status='draft'", (edit_id,),
    )
    await conn.commit()
    if changed.rowcount != 1:
        raise RuntimeError("Only an awaiting-approval draft can be discarded.")
