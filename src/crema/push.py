"""Approve/push and discard drafted profile edits.

Pushing creates a NEW profile on the machine (the vendored client appends the
" [AI]" label suffix), so the user's original profile is never overwritten. The
new profile is saved but not auto-selected — you pick it on the machine.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from gaggimate_mcp.api.websocket import GaggimateWebSocketClient
from gaggimate_mcp.errors import GaggimateError

from . import db
from .config import CremaConfig


async def push_edit(conn: aiosqlite.Connection, config: CremaConfig, edit_id: int) -> dict[str, Any]:
    """Push an approved edit to the machine as a new profile.

    Returns the updated edit dict. Raises if the edit isn't in 'draft' state.
    """
    edit = await db.get_pending_edit(conn, edit_id)
    if edit is None:
        raise RuntimeError(f"Edit {edit_id} not found.")
    if edit["status"] != "draft":
        raise RuntimeError(f"Edit {edit_id} is '{edit['status']}', not a draft — nothing to push.")

    profile = edit["profile"]
    ws = GaggimateWebSocketClient(config.gaggimate())
    try:
        saved = await ws.create_or_update_profile(
            label=profile["label"],
            temperature=profile["temperature"],
            phases=profile["phases"],
            profile_id=None,  # always create new — never overwrite the original
            profile_type=profile.get("type", "pro"),
        )
    except GaggimateError as e:
        await db.set_edit_status(conn, edit_id, "failed", error=str(e))
        raise RuntimeError(f"Push failed: {e}") from e

    device_id = str(saved.get("id", "")) or None
    await db.set_edit_status(conn, edit_id, "pushed", device_profile_id=device_id)
    result = await db.get_pending_edit(conn, edit_id)
    assert result is not None
    return result


async def discard_edit(conn: aiosqlite.Connection, edit_id: int) -> None:
    """Mark a drafted edit as discarded."""
    await db.set_edit_status(conn, edit_id, "discarded")
