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
from .review import review_recent

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
def review() -> None:
    """Ingest new shots, then run a Claude review of the recent window."""

    async def _run() -> None:
        cfg = _config()
        conn = await db.connect(cfg.db_path)
        try:
            new_ids = await ingest_new_shots(conn, cfg)
            typer.echo(f"Ingested {len(new_ids)} new shot(s).")
            result = await review_recent(conn, cfg)
            if result is None:
                typer.echo("No shots available to review yet.")
                return
            typer.echo(json.dumps(result["suggestions"], indent=2))
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def draft(review_id: Optional[int] = typer.Argument(None, help="Review id (default: latest).")) -> None:
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
            edit = await draft_from_review(conn, cfg, rid)
            typer.echo(f"Drafted edit #{edit['id']} from review {rid}:")
            typer.echo(edit["change_summary"])
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
