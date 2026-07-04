"""crema web report.

Shows machine status, the latest Claude review, drafted profile edits, and recent
shots — with buttons to run a review, analyze a specific shot, draft a profile
edit (optionally for a chosen profile), and approve/discard edits.

LAN-exposed: optional HTTP Basic auth + a same-origin CSRF guard on POST routes.
Pure inline CSS, no JavaScript or external assets — light on the Pi.
"""

from __future__ import annotations

import asyncio
import html
import secrets
from typing import Any, Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import db
from ..config import CremaConfig
from ..draft import draft_from_review
from ..ingest import ingest_new_shots
from ..push import discard_edit, push_edit
from ..review import review_recent, review_shots

_cfg = CremaConfig()
_security = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_security)) -> None:
    """Gate all routes behind HTTP Basic auth when a web password is configured."""
    if not _cfg.web_password:
        return
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, _cfg.web_user)
        and secrets.compare_digest(credentials.password, _cfg.web_password)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def require_same_origin(request: Request) -> None:
    """CSRF guard: state-changing requests must originate from this site."""
    if request.method in _SAFE_METHODS:
        return
    host = (request.headers.get("host") or "").lower()
    origin = request.headers.get("origin")
    if origin is not None:
        if urlparse(origin).netloc.lower() == host:
            return
    else:
        referer = request.headers.get("referer")
        if referer and urlparse(referer).netloc.lower() == host:
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request blocked")


app = FastAPI(title="crema", dependencies=[Depends(require_auth), Depends(require_same_origin)])


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#f3efe7; --surface:#fffdf9; --surface-2:#f7f2ea; --text:#2a231c;
  --muted:#8a7d6b; --border:#e7e0d4; --accent:#b5551d; --accent-ink:#fff;
  --ok:#3f8f43; --off:#c0392b; --lo:#c2871a; --mid:#3b74c4; --hi:#3f8f43;
  --shadow:0 1px 2px rgba(40,30,15,.05),0 6px 20px rgba(40,30,15,.05);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#17130f; --surface:#221d17; --surface-2:#1c1712; --text:#ece4d8;
  --muted:#a2937e; --border:#332b22; --accent:#dd7d40; --accent-ink:#1a120b;
  --ok:#6cbf6f; --off:#e0715f; --lo:#dca23e; --mid:#6aa2e6; --hi:#6cbf6f;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.28);
}}
:root[data-theme=light]{
  --bg:#f3efe7; --surface:#fffdf9; --surface-2:#f7f2ea; --text:#2a231c;
  --muted:#8a7d6b; --border:#e7e0d4; --accent:#b5551d; --accent-ink:#fff;
  --ok:#3f8f43; --off:#c0392b; --lo:#c2871a; --mid:#3b74c4; --hi:#3f8f43;
  --shadow:0 1px 2px rgba(40,30,15,.05),0 6px 20px rgba(40,30,15,.05);
}
:root[data-theme=dark]{
  --bg:#17130f; --surface:#221d17; --surface-2:#1c1712; --text:#ece4d8;
  --muted:#a2937e; --border:#332b22; --accent:#dd7d40; --accent-ink:#1a120b;
  --ok:#6cbf6f; --off:#e0715f; --lo:#dca23e; --mid:#6aa2e6; --hi:#6cbf6f;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.28);
}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.6 ui-sans-serif,-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:54rem;margin:0 auto;padding:1.4rem 1.1rem 4rem}
header{display:flex;align-items:center;justify-content:space-between;gap:1rem;
  margin-bottom:1.3rem}
h1{font-size:1.5rem;margin:0;letter-spacing:-.01em;font-weight:700}
h2{font-size:.82rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:700;margin:1.8rem .2rem .6rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:1.05rem 1.2rem;margin:.7rem 0;box-shadow:var(--shadow)}
.muted{color:var(--muted)}
.row{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.2rem .8rem;margin:.5rem 0}
.kv .k{color:var(--muted);font-weight:600}
.lead{font-size:1.05rem;font-weight:600;margin:.1rem 0 .5rem}
.pill{display:inline-flex;align-items:center;gap:.35rem;padding:.16rem .6rem;
  border-radius:999px;font-size:.76rem;font-weight:700;line-height:1;
  border:1px solid color-mix(in srgb,currentColor 35%,transparent);
  background:color-mix(in srgb,currentColor 12%,transparent)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:currentColor}
.c-ok{color:var(--ok)} .c-off{color:var(--off)} .c-accent{color:var(--accent)}
.c-lo{color:var(--lo)} .c-mid{color:var(--mid)} .c-hi{color:var(--hi)}
.c-muted{color:var(--muted)}
.btn{font:inherit;font-weight:600;background:var(--accent);color:var(--accent-ink);
  border:1px solid transparent;border-radius:10px;padding:.5rem .95rem;cursor:pointer;
  transition:filter .12s}
.btn:hover{filter:brightness(1.06)}
.btn-ghost{background:transparent;color:var(--text);border-color:var(--border)}
.btn-ghost:hover{background:var(--surface-2);filter:none}
.btn-sm{padding:.24rem .6rem;font-size:.82rem;border-radius:8px}
select{font:inherit;color:var(--text);background:var(--surface-2);
  border:1px solid var(--border);border-radius:8px;padding:.35rem .5rem}
ul.changes{margin:.3rem 0;padding-left:1.1rem}
ul.changes li{margin:.25rem 0}
code{background:var(--surface-2);border:1px solid var(--border);border-radius:6px;
  padding:.05rem .35rem;font-size:.9em}
pre{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;
  padding:.7rem .85rem;margin:.5rem 0;white-space:pre-wrap;font-size:.9rem;overflow-x:auto}
.banner{border-left:4px solid;padding:.7rem 1rem;border-radius:10px;margin:.7rem 0;
  background:var(--surface)}
.banner.error{border-color:var(--off);color:var(--off)}
.banner.note{border-color:var(--muted);color:var(--muted)}
.shot{display:flex;align-items:center;justify-content:space-between;gap:.8rem}
.shot .meta{min-width:0}
form.inline{display:inline;margin:0}
"""


def _page(body: str) -> str:
    return f"<style>{_CSS}</style><div class='wrap'>{body}</div>"


def _pill(text: str, cls: str, dot: bool = False) -> str:
    d = "<span class='dot'></span>" if dot else ""
    return f"<span class='pill {cls}'>{d}{html.escape(text)}</span>"


async def _machine_status() -> tuple[bool, str]:
    """Fast reachability probe: a short TCP connect (not a request). 1.5s timeout."""
    g = _cfg.gaggimate()
    host, port = g.host, (443 if g.use_https else 80)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True, host
    except Exception:  # noqa: BLE001
        return False, host


def _profiles_in_shots(shots: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Distinct (id, name) profiles seen across the recent shots, newest first."""
    seen: dict[str, str] = {}
    for sh in shots:
        t = sh["transformed"]
        pid = t.get("profile_id")
        if pid and str(pid) not in seen:
            seen[str(pid)] = str(t.get("profile_name") or pid)
    return [{"id": pid, "name": name} for pid, name in seen.items()]


def _draft_form(review_id: int, profiles: list[dict[str, str]]) -> str:
    """Draft button, with a profile picker when the window spans >1 profile."""
    if len(profiles) > 1:
        options = "".join(
            f"<option value='{html.escape(p['id'])}'>{html.escape(p['name'])}</option>" for p in profiles
        )
        picker = f"<label class='muted'>for profile </label><select name='profile_id'>{options}</select> "
    elif len(profiles) == 1:
        picker = f"<input type='hidden' name='profile_id' value='{html.escape(profiles[0]['id'])}'>"
    else:
        picker = ""
    return (
        "<form method='post' action='/draft' class='row' style='margin-top:.8rem'>"
        f"<input type='hidden' name='review_id' value='{review_id}'>"
        f"{picker}<button class='btn btn-sm' type='submit'>Draft profile edit</button></form>"
    )


_CONF_CLS = {"low": "c-lo", "medium": "c-mid", "high": "c-hi"}


def _render_review(review: Optional[dict[str, Any]], profiles: list[dict[str, str]]) -> str:
    if review is None:
        return "<div class='card muted'>No review yet — click <b>Run review</b> after pulling a shot.</div>"
    s = review["suggestions"]
    conf = str(s.get("confidence", ""))
    changes = s.get("profile_changes") or []
    if changes:
        changes_html = "<ul class='changes'>" + "".join(
            f"<li><b>{html.escape(str(c.get('phase') or 'profile'))}</b> · "
            f"{html.escape(c.get('parameter', ''))}: <code>{html.escape(c.get('change', ''))}</code> — "
            f"{html.escape(c.get('reason', ''))}</li>"
            for c in changes
        ) + "</ul>"
    else:
        changes_html = "<p class='muted'>No profile changes suggested.</p>"
    return f"""<div class="card">
      <div class="row" style="justify-content:space-between">
        <span class="lead">{html.escape(s.get('diagnosis', ''))}</span>
        {_pill(conf + ' confidence', _CONF_CLS.get(conf, 'c-muted')) if conf else ''}
      </div>
      <div class="kv">
        <span class="k">Grind</span><span>{html.escape(s.get('grind_change', ''))}</span>
        <span class="k">Dose / yield</span><span>{html.escape(s.get('dose_yield_change', ''))}</span>
      </div>
      <div class="k muted" style="font-weight:600;margin-top:.4rem">Profile changes</div>
      {changes_html}
      <p class="muted" style="margin:.5rem 0 0">{html.escape(s.get('rationale', ''))}</p>
      <p class="muted" style="font-size:.85rem;margin:.4rem 0 0">
        {html.escape(review['model'])} · newest shot {html.escape(review['shot_id'])}</p>
      {_draft_form(review['id'], profiles)}
    </div>"""


_EDIT_PILL = {"draft": "c-accent", "pushed": "c-hi", "discarded": "c-muted", "failed": "c-off"}


def _render_edits(edits: list[dict[str, Any]]) -> str:
    if not edits:
        return "<div class='card muted'>No profile edits yet — draft one from a review above.</div>"
    out = []
    for e in edits:
        phases = e["profile"].get("phases", [])
        if e["status"] == "draft":
            actions = (
                f"<form method='post' action='/edits/{e['id']}/push' class='inline'>"
                f"<button class='btn btn-sm' type='submit'>Approve &amp; push</button></form> "
                f"<form method='post' action='/edits/{e['id']}/discard' class='inline'>"
                f"<button class='btn btn-sm btn-ghost' type='submit'>Discard</button></form>"
            )
        elif e["status"] == "pushed":
            did = f" (id {html.escape(e['device_profile_id'])})" if e["device_profile_id"] else ""
            actions = (
                f"<p class='muted' style='margin:.5rem 0 0'>Saved to machine as "
                f"<code>{html.escape(e['label'])} [AI]</code>{did} — select it on the machine to use it.</p>"
            )
        elif e["status"] == "failed":
            actions = f"<p class='c-off' style='margin:.5rem 0 0'>Push failed: {html.escape(e['error'] or '')}</p>"
        else:
            actions = ""
        out.append(
            f"""<div class="card">
              <div class="row" style="justify-content:space-between">
                <span>{_pill(e['status'], _EDIT_PILL.get(e['status'], 'c-muted'))}
                  <b>Edit #{e['id']}</b>
                  <span class="muted">· base {html.escape(e['base_profile_label'] or '—')} · {len(phases)} phase(s)</span></span>
              </div>
              <pre>{html.escape(e['change_summary'])}</pre>
              <div class="row">{actions}</div>
            </div>"""
        )
    return "".join(out)


def _render_shots(shots: list[dict[str, Any]]) -> str:
    if not shots:
        return "<div class='card muted'>No shots ingested yet.</div>"
    rows = []
    for sh in shots:
        t = sh["transformed"]
        rows.append(
            f"""<div class="card shot">
              <span class="meta"><b>Shot {html.escape(sh['id'])}</b>
                <span class="muted">· {html.escape(str(t.get('profile_name', '—')))}
                · {html.escape(str(t.get('duration_seconds', '—')))}s
                · {html.escape(str(t.get('final_weight_g', '—')))}g</span></span>
              <form method="post" action="/analyze" class="inline">
                <input type="hidden" name="shot_id" value="{html.escape(sh['id'])}">
                <button class="btn btn-sm btn-ghost" type="submit">Analyze</button>
              </form>
            </div>"""
        )
    return "".join(rows)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def _redirect_error(exc: Exception) -> RedirectResponse:
    msg = str(exc) or exc.__class__.__name__
    return RedirectResponse(f"/?error={quote(msg[:300])}", status_code=303)


@app.exception_handler(404)
@app.exception_handler(405)
async def _to_home(request: Request, exc: Exception) -> RedirectResponse:
    """A stray GET on an action route, or unknown path, just goes to the report."""
    return RedirectResponse("/", status_code=303)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> HTMLResponse:
    body = (
        "<header><h1>crema ☕</h1></header>"
        f"<div class='banner error'>Something went wrong: {html.escape(str(exc))}</div>"
        "<p><a href='/'>← back to the report</a></p>"
    )
    return HTMLResponse(_page(body), status_code=500)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index(error: Optional[str] = None, note: Optional[str] = None) -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        review = await db.latest_review(conn)
        shots = await db.recent_shots(conn, limit=_cfg.review_window)
        edits = await db.list_pending_edits(conn, limit=10)
        autoreview = await db.get_bool_setting(conn, "autoreview", _cfg.autoreview)
    finally:
        await conn.close()
    auto_pill = _pill(f"auto-review {'on' if autoreview else 'off'}", "c-ok" if autoreview else "c-muted", dot=True)
    auto_toggle = (
        f"<form method='post' action='/autoreview' class='inline'>"
        f"<input type='hidden' name='on' value='{0 if autoreview else 1}'>"
        f"<button class='btn btn-sm btn-ghost' type='submit'>Turn {'off' if autoreview else 'on'}</button></form>"
    )
    reachable, host = await _machine_status()
    status_pill = _pill(
        f"machine {'online' if reachable else 'off'}", "c-ok" if reachable else "c-off", dot=True
    )
    banner = ""
    if error:
        banner += f"<div class='banner error'>{html.escape(error)}</div>"
    if note:
        banner += f"<div class='banner note'>{html.escape(note)}</div>"
    body = f"""
      <header>
        <h1>crema ☕</h1>
        <span class="row">{status_pill}<span class="muted" style="font-size:.82rem">{html.escape(host)}</span></span>
      </header>
      {banner}
      <div class="row">
        <form method="post" action="/review" class="inline"><button class="btn" type="submit">Run review</button></form>
        {auto_pill}{auto_toggle}
      </div>
      <h2>Latest review</h2>{_render_review(review, _profiles_in_shots(shots))}
      <h2>Profile edits</h2>{_render_edits(edits)}
      <h2>Recent shots</h2>{_render_shots(shots)}
    """
    return _page(body)


@app.post("/review")
async def run_review() -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        new_ids = await ingest_new_shots(conn, _cfg)
        if not new_ids and await db.latest_review(conn) is not None:
            return RedirectResponse(
                "/?note=" + quote("No new shots since the last review — nothing to review."),
                status_code=303,
            )
        await review_recent(conn, _cfg)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/autoreview")
async def toggle_autoreview(on: str = Form(...)) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await db.set_setting(conn, "autoreview", "1" if on == "1" else "0")
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/analyze")
async def run_analyze(shot_id: str = Form(...)) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        result = await review_shots(conn, _cfg, [shot_id])
        if result is None:
            return RedirectResponse("/?error=" + quote(f"Shot {shot_id} not found."), status_code=303)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/draft")
async def run_draft(
    review_id: int = Form(...), profile_id: Optional[str] = Form(None)
) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await draft_from_review(conn, _cfg, review_id, profile_id=profile_id or None)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/push")
async def approve_edit(edit_id: int) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await push_edit(conn, _cfg, edit_id)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/discard")
async def reject_edit(edit_id: int) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await discard_edit(conn, edit_id)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)
