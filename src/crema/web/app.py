"""Phase 1 read-only web report.

Shows the latest Claude review and the recent shots that fed it, with a button to
run a fresh review (ingest + Claude). No writes to the machine happen here —
pushing profile edits back is Phase 2.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..config import CremaConfig
from ..ingest import ingest_new_shots
from ..review import review_recent

app = FastAPI(title="crema")
_cfg = CremaConfig()


def _page(body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>crema</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; max-width: 46rem;
         margin: 2rem auto; padding: 0 1rem; color: #1c1a17; background: #f4f1ea; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
  .card {{ background: #fff; border: 1px solid #e5e0d5; border-radius: 10px;
           padding: 1rem 1.2rem; margin: 0.8rem 0; }}
  .k {{ color: #8a7f6d; font-weight: 600; }}
  button {{ font: inherit; background: #b5551d; color: #fff; border: 0;
            border-radius: 8px; padding: .5rem 1rem; cursor: pointer; }}
  code, pre {{ background: #f0ece2; border-radius: 6px; padding: .1rem .3rem; }}
  ul {{ padding-left: 1.1rem; }} .muted {{ color: #8a7f6d; }}
</style></head><body>{body}</body></html>"""


def _render_review(review: dict[str, Any] | None) -> str:
    if review is None:
        return '<div class="card muted">No review yet. Click “Run review”.</div>'
    s = review["suggestions"]
    changes = s.get("profile_changes") or []
    changes_html = (
        "<ul>"
        + "".join(
            f"<li><span class='k'>{html.escape(str(c.get('phase') or 'profile'))}</span> · "
            f"{html.escape(c.get('parameter', ''))}: <code>{html.escape(c.get('change', ''))}</code> — "
            f"{html.escape(c.get('reason', ''))}</li>"
            for c in changes
        )
        + "</ul>"
        if changes
        else "<p class='muted'>No profile changes suggested.</p>"
    )
    return f"""<div class="card">
      <p><span class="k">Diagnosis:</span> {html.escape(s.get('diagnosis', ''))}</p>
      <p><span class="k">Grind:</span> {html.escape(s.get('grind_change', ''))}</p>
      <p><span class="k">Dose / yield:</span> {html.escape(s.get('dose_yield_change', ''))}</p>
      <p><span class="k">Profile changes:</span></p>{changes_html}
      <p><span class="k">Confidence:</span> {html.escape(s.get('confidence', ''))}</p>
      <p class="muted">{html.escape(s.get('rationale', ''))}</p>
      <p class="muted">Model: {html.escape(review['model'])} · newest shot {html.escape(review['shot_id'])}</p>
    </div>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        review = await db.latest_review(conn)
        shots = await db.recent_shots(conn, limit=_cfg.review_window)
    finally:
        await conn.close()
    shots_html = "".join(
        f"<div class='card'><span class='k'>Shot {html.escape(sh['id'])}</span> · "
        f"profile {html.escape(str(sh['transformed'].get('profile_name', '—')))} · "
        f"{html.escape(str(sh['transformed'].get('duration_seconds', '—')))}s · "
        f"{html.escape(str(sh['transformed'].get('final_weight_g', '—')))}g</div>"
        for sh in shots
    ) or "<div class='card muted'>No shots ingested yet.</div>"
    body = f"""
      <h1>crema ☕</h1>
      <form method="post" action="/review"><button type="submit">Run review</button></form>
      <h2>Latest review</h2>{_render_review(review)}
      <h2>Recent shots</h2>{shots_html}
    """
    return _page(body)


@app.post("/review")
async def run_review() -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await ingest_new_shots(conn, _cfg)
        await review_recent(conn, _cfg)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)
