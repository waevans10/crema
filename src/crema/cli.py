"""crema command-line interface.

    crema ingest    # pull new shots from the machine into the DB
    crema review    # ingest, then run a Claude review and print it
    crema serve     # run the read-only web report
"""

from __future__ import annotations

import asyncio
import json

import typer

from typing import Optional

from . import db
from .config import CremaConfig
from .doctor import run_checks
from .draft import draft_from_review
from .ingest import ingest_new_shots
from .push import discard_edit, push_edit
from .review import review_recent, review_shots

app = typer.Typer(add_completion=False, help="Automated GaggiMate shot reviewer.")


def _config() -> CremaConfig:
    return CremaConfig()


@app.command()
def doctor() -> None:
    """Check device HTTP, device WebSocket, and Claude API connectivity."""

    async def _run() -> None:
        checks = await run_checks(_config())
        for c in checks:
            mark = "✓" if c.ok else "✗"
            typer.echo(f"  {mark}  {c.name:28} {c.detail}")
        if all(c.ok for c in checks):
            typer.echo("\nAll good — crema can reach the machine and Claude.")
        else:
            typer.echo("\nSome checks failed (see above).")
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command()
def ingest() -> None:
    """Fetch new shots from the device and store them."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            new_ids = await ingest_new_shots(conn, cfg)
            typer.echo(f"Ingested {len(new_ids)} new shot(s): {', '.join(new_ids) or '—'}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def review(
    force: bool = typer.Option(
        False, "--force", "-f", help="Review even if no new shots came in (costs an API call)."
    ),
) -> None:
    """Ingest new shots, then run a Claude review of the recent window.

    To keep costs down, this skips the (paid) Claude call when no new shots were
    ingested and a review already exists — so the scheduled timer only spends
    money when you've actually pulled a shot. Use --force to review anyway.
    """

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            new_ids = await ingest_new_shots(conn, cfg)
            typer.echo(f"Ingested {len(new_ids)} new shot(s).")
            if not force:
                if not await db.get_bool_setting(conn, "autoreview", cfg.autoreview):
                    typer.echo("Auto-review is off — skipping (turn it on in the UI, or use --force).")
                    return
                if not new_ids and await db.latest_review(conn) is not None:
                    typer.echo("No new shots since the last review — skipping (use --force to override).")
                    return
            result = await review_recent(conn, cfg)
            if result is None:
                typer.echo("No shots available to review yet.")
                return
            typer.echo(json.dumps(result["suggestions"], indent=2))
            u = result.get("usage")
            if u:
                typer.echo(
                    f"\n[tokens: {u['input_tokens']} in / {u['output_tokens']} out on {result['model']}]"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def autoreview(
    state: Optional[str] = typer.Argument(None, help="on | off (omit to show current state)."),
) -> None:
    """Turn automatic review of new shots on or off (governs the scheduled timer)."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            if state is None:
                on = await db.get_bool_setting(conn, "autoreview", cfg.autoreview)
                typer.echo(f"Auto-review is {'ON' if on else 'OFF'}.")
                return
            s = state.strip().lower()
            if s not in ("on", "off"):
                typer.echo("Usage: crema autoreview [on|off]")
                raise typer.Exit(code=1)
            await db.set_setting(conn, "autoreview", "1" if s == "on" else "0")
            typer.echo(f"Auto-review turned {s.upper()}.")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def grinder(
    description: Optional[str] = typer.Argument(
        None, help='Your grinder, in your own words (omit to show the current setting). Pass "" to clear.'
    ),
) -> None:
    """Describe your grinder so reviews give grind advice in its own steps/clicks."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            if description is None:
                current = (await db.get_setting(conn, "grinder")) or cfg.grinder
                typer.echo(f"Grinder: {current}" if current else "No grinder set.")
                return
            await db.set_setting(conn, "grinder", description.strip()[:300])
            typer.echo(f"Grinder set to: {description.strip()}" if description.strip() else "Grinder cleared.")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def taste(
    shot_id: str = typer.Argument(..., help="Shot id the notes are about (e.g. 000091)."),
    notes: Optional[str] = typer.Argument(
        None, help='How it tasted (omit to show the current notes). Pass "" to clear.'
    ),
) -> None:
    """Record how a shot tasted; future reviews weigh it alongside the telemetry."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        sid = shot_id.zfill(6)
        try:
            if notes is None:
                shot = await db.get_shot(conn, sid)
                if shot is None:
                    typer.echo(f"Shot {sid} not found — run `crema ingest` first.")
                    return
                current = shot.get("tasting_notes")
                typer.echo(f"Shot {sid} tasting notes: {current}" if current else f"No tasting notes on shot {sid}.")
                return
            found = await db.set_shot_tasting_notes(conn, sid, notes.strip()[:500] or None)
            if not found:
                typer.echo(f"Shot {sid} not found — run `crema ingest` first.")
                return
            typer.echo(
                f"Tasting notes saved for shot {sid} — the next review will take them into account."
                if notes.strip()
                else f"Tasting notes cleared for shot {sid}."
            )
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def analyze(shot_id: str = typer.Argument(..., help="Shot id to analyze (e.g. 000091).")) -> None:
    """Run a Claude review of one specific shot."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            result = await review_shots(conn, cfg, [shot_id.zfill(6)])
            if result is None:
                typer.echo(f"Shot {shot_id} not found — run `crema ingest` first.")
                return
            typer.echo(json.dumps(result["suggestions"], indent=2))
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def draft(
    review_id: Optional[int] = typer.Argument(None, help="Review id (default: latest)."),
    profile_id: Optional[str] = typer.Option(
        None, "--profile-id", help="Profile to base the edit on (default: the review's newest shot's)."
    ),
    notes: Optional[str] = typer.Option(
        None, "--notes", help="Your own feedback for Claude (taste, preferences, constraints)."
    ),
) -> None:
    """Draft a profile edit from a review (stored as a pending edit, not pushed)."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            rid = review_id
            if rid is None:
                latest = await db.latest_review(conn)
                if latest is None:
                    typer.echo("No reviews to draft from. Run `crema review` first.")
                    return
                rid = latest["id"]
            edit = await draft_from_review(conn, cfg, rid, profile_id=profile_id, user_notes=notes)
            typer.echo(f"Drafted edit #{edit['id']} from review {rid}:")
            typer.echo(edit["change_summary"])
            if edit["stop_changes"]:
                typer.echo("\n⚠ Stop conditions changed vs the base profile:")
                for c in edit["stop_changes"]:
                    typer.echo(f"  - {c}")
                typer.echo("Review these carefully before pushing.")
            typer.echo(f"\nApprove with:  crema push {edit['id']}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def edits(status: Optional[str] = typer.Option(None, help="Filter: draft|pushed|discarded|failed")) -> None:
    """List profile edits."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            for e in await db.list_pending_edits(conn, status=status):
                typer.echo(f"#{e['id']} [{e['status']}] base={e['base_profile_label']} — {e['label']}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def push(edit_id: int = typer.Argument(..., help="Edit id to approve and push.")) -> None:
    """Push an approved edit to the machine as a new [AI] profile."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            result = await push_edit(conn, cfg, edit_id)
            typer.echo(
                f"Pushed edit #{edit_id} → saved on machine as "
                f"'{result['label']} [AI]'"
                + (f" (id {result['device_profile_id']})" if result["device_profile_id"] else "")
                + ". Select it on the machine to use it."
            )
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def discard(edit_id: int = typer.Argument(..., help="Edit id to discard.")) -> None:
    """Discard a drafted edit."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            await discard_edit(conn, edit_id)
            typer.echo(f"Discarded edit #{edit_id}.")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def serve() -> None:
    """Run the read-only web report (Phase 1)."""
    import uvicorn

    cfg = _config()
    if cfg.host in ("127.0.0.1", "localhost"):
        typer.echo(
            f"Serving on {cfg.host}:{cfg.port} (loopback). To view from another machine, "
            f"SSH-forward it:\n  ssh -L {cfg.port}:localhost:{cfg.port} <user>@<this-pi>\n"
            f"then open http://localhost:{cfg.port}\n"
        )
    uvicorn.run("crema.web.app:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    app()
