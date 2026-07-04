"""Phase 1 read-only web report.

Shows the latest Claude review and the recent shots that fed it, with a button to
run a fresh review (ingest + Claude). No writes to the machine happen here —
pushing profile edits back is Phase 2.
"""

from __future__ import annotations

import html
import secrets
from typing import Any, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import db
from ..config import CremaConfig
from ..draft import draft_from_review
from ..ingest import ingest_new_shots
from ..push import discard_edit, push_edit
from ..review import review_recent

_cfg = CremaConfig()
_security = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_security)) -> None:
    """Gate all routes behind HTTP Basic auth when a web password is configured."""
    if not _cfg.web_password:
        return  # auth disabled (loopback default)
    valid = credentials is not None and secrets.compare_digest(
        credentials.username, _cfg.web_user
    ) and secrets.compare_digest(credentials.password, _cfg.web_password)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="crema", dependencies=[Depends(require_auth)])

_STATUS_STYLE = {
    "draft": "#b5551d",
    "pushed": "#2e7d32",
    "discarded": "#8a7f6d",
    "failed": "#c62828",
}


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
      <form method="post" action="/draft" style="margin-top:.6rem">
        <input type="hidden" name="review_id" value="{review['id']}">
        <button type="submit">Draft profile edit</button>
      </form>
    </div>"""


def _render_edits(edits: list[dict[str, Any]]) -> str:
    if not edits:
        return '<div class="card muted">No profile edits yet. Draft one from a review above.</div>'
    out = []
    for e in edits:
        color = _STATUS_STYLE.get(e["status"], "#8a7f6d")
        phases = e["profile"].get("phases", [])
        actions = ""
        if e["status"] == "draft":
            actions = f"""
              <form method="post" action="/edits/{e['id']}/push" style="display:inline">
                <button type="submit">Approve &amp; push to machine</button></form>
              <form method="post" action="/edits/{e['id']}/discard" style="display:inline;margin-left:.5rem">
                <button type="submit" style="background:#8a7f6d">Discard</button></form>"""
        elif e["status"] == "pushed":
            actions = (
                f"<p class='muted'>Saved to machine as "
                f"<code>{html.escape(e['label'])} [AI]</code>"
                + (f" (id {html.escape(e['device_profile_id'])})" if e["device_profile_id"] else "")
                + " — select it on the machine to use it.</p>"
            )
        elif e["status"] == "failed":
            actions = f"<p style='color:#c62828'>Push failed: {html.escape(e['error'] or '')}</p>"
        out.append(
            f"""<div class="card">
              <p><span class="k">Edit #{e['id']}</span>
                 <span style="color:{color};font-weight:600">[{html.escape(e['status'])}]</span>
                 · base: {html.escape(e['base_profile_label'] or '—')} · {len(phases)} phase(s)</p>
              <pre style="white-space:pre-wrap">{html.escape(e['change_summary'])}</pre>
              {actions}
            </div>"""
        )
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        review = await db.latest_review(conn)
        shots = await db.recent_shots(conn, limit=_cfg.review_window)
        edits = await db.list_pending_edits(conn, limit=10)
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
      <h2>Profile edits</h2>{_render_edits(edits)}
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


@app.post("/draft")
async def run_draft(review_id: int = Form(...)) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await draft_from_review(conn, _cfg, review_id)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/push")
async def approve_edit(edit_id: int) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await push_edit(conn, _cfg, edit_id)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/discard")
async def reject_edit(edit_id: int) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await discard_edit(conn, edit_id)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)
