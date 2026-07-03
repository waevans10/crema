"""crema command-line interface.

    crema ingest    # pull new shots from the machine into the DB
    crema review    # ingest, then run a Claude review and print it
    crema serve     # run the read-only web report
"""

from __future__ import annotations

import asyncio
import json

import typer

from . import db
from .config import CremaConfig
from .ingest import ingest_new_shots
from .review import review_recent

app = typer.Typer(add_completion=False, help="Automated GaggiMate shot reviewer.")


def _config() -> CremaConfig:
    return CremaConfig()


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
