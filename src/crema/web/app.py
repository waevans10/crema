"""crema web report.

Shows machine status, the latest Claude review, drafted profile edits, and recent
shots — with buttons to run a review, analyze a specific shot, draft a profile
edit (optionally for a chosen profile), and approve/discard edits.

LAN-exposed: optional HTTP Basic auth + a same-origin CSRF guard on POST routes.
Pure inline CSS, no JavaScript or external assets — light on the Pi.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
.banner.warn{border-color:var(--lo);color:var(--lo);background:var(--surface)}
.banner.warn ul{margin:.3rem 0 0;padding-left:1.2rem}
.shot{display:flex;align-items:center;justify-content:space-between;gap:.8rem}
.shot .meta{min-width:0}
form.inline{display:inline;margin:0}
.brand{display:flex;align-items:center;gap:.65rem;text-decoration:none;color:inherit}
.brand img{width:32px;height:32px;border-radius:9px;display:block}
nav.toc{display:flex;gap:1rem;margin:.2rem 0 0;font-size:.85rem}
nav.toc a{color:var(--muted);text-decoration:none;font-weight:600}
nav.toc a:hover{color:var(--accent)}
.score{display:inline-flex;align-items:baseline;gap:.15rem;font-weight:800;
  font-size:1.5rem;line-height:1;padding:.42rem .6rem;border-radius:12px;
  border:1px solid color-mix(in srgb,currentColor 35%,transparent);
  background:color-mix(in srgb,currentColor 12%,transparent)}
.score small{font-size:.72rem;font-weight:700;opacity:.75}
.review-top{display:flex;gap:.9rem;align-items:flex-start}
textarea{font:inherit;color:var(--text);background:var(--surface-2);width:100%;
  border:1px solid var(--border);border-radius:8px;padding:.45rem .6rem;resize:vertical}
textarea::placeholder{color:var(--muted);opacity:.8}
label.ack{display:flex;align-items:center;gap:.45rem;font-size:.88rem;color:var(--lo);
  font-weight:600;margin:.2rem 0}
@media (max-width:640px){
  header{flex-direction:column;align-items:flex-start;gap:.4rem}
  .kv{grid-template-columns:1fr}
  .kv .k{margin-top:.3rem}
  .shot{flex-direction:column;align-items:flex-start}
}
"""


def _page(body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>crema · shot report</title>"
        "<link rel='icon' type='image/png' href='/icon.png'>"
        "<link rel='apple-touch-icon' href='/icon.png'>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>{body}</div></body></html>"
    )


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
    """Draft form: optional barista notes + profile picker when the window spans >1 profile."""
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
        "<form method='post' action='/draft' style='margin-top:.8rem'>"
        f"<input type='hidden' name='review_id' value='{review_id}'>"
        "<textarea name='notes' rows='2' placeholder='Optional notes for Claude — how it tasted, "
        "what you want (e.g. came out sour; keep preinfusion under 6s)'></textarea>"
        f"<div class='row' style='margin-top:.5rem'>{picker}"
        "<button class='btn btn-sm' type='submit'>Draft a profile edit</button></div></form>"
    )


_CONF_CLS = {"low": "c-lo", "medium": "c-mid", "high": "c-hi"}


def _fmt_ts(ts: Any) -> str:
    """Unix seconds → 'Sat Jul 04, 08:15' in local time, or '' when unknown."""
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%a %b %d, %H:%M")
    except (ValueError, OverflowError, OSError):
        return ""


def _score_badge(score: Any) -> str:
    """The review's 1-10 shot score as a colored badge (red/amber/green bands)."""
    try:
        n = int(score)
    except (TypeError, ValueError):
        return ""
    cls = "c-off" if n <= 3 else "c-lo" if n <= 6 else "c-hi"
    return f"<span class='score {cls}' title='Shot quality score'>{n}<small>/10</small></span>"


def _render_review(review: Optional[dict[str, Any]], profiles: list[dict[str, str]]) -> str:
    if review is None:
        return "<div class='card muted'>No review yet — click <b>Review new shots</b> after pulling a shot.</div>"
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
        changes_html = (
            "<p class='muted'>No profile changes suggested — dial in the bench changes "
            "above first, then review the next shot.</p>"
        )
    reviewed_at = _fmt_ts(review.get("created_at"))
    when = f" · reviewed {html.escape(reviewed_at)}" if reviewed_at else ""
    draft_form = _draft_form(review["id"], profiles) if changes else ""
    return f"""<div class="card">
      <div class="review-top">
        {_score_badge(s.get('score'))}
        <div style="flex:1;min-width:0">
          <div class="row" style="justify-content:space-between">
            <span class="lead">{html.escape(s.get('diagnosis', ''))}</span>
            {_pill(conf + ' confidence', _CONF_CLS.get(conf, 'c-muted')) if conf else ''}
          </div>
        </div>
      </div>
      <div class="kv">
        <span class="k">Grind</span><span>{html.escape(s.get('grind_change', ''))}</span>
        <span class="k">Dose / yield</span><span>{html.escape(s.get('dose_yield_change', ''))}</span>
      </div>
      <div class="k muted" style="font-weight:600;margin-top:.4rem">Profile changes</div>
      {changes_html}
      <p class="muted" style="margin:.5rem 0 0">{html.escape(s.get('rationale', ''))}</p>
      <p class="muted" style="font-size:.85rem;margin:.4rem 0 0">
        shot {html.escape(review['shot_id'])}{when} · {html.escape(review['model'])}</p>
      {draft_form}
    </div>"""


_EDIT_PILL = {"draft": "c-accent", "pushed": "c-hi", "discarded": "c-muted", "failed": "c-off"}
_EDIT_LABEL = {
    "draft": "awaiting approval",
    "pushed": "pushed to machine",
    "discarded": "discarded",
    "failed": "push failed",
}


def _stop_warning(e: dict[str, Any]) -> str:
    """Explicit disclosure when a draft changes the shot's stop conditions."""
    if not e.get("stop_changes"):
        return ""
    items = "".join(f"<li>{html.escape(c)}</li>" for c in e["stop_changes"])
    return (
        "<div class='banner warn'><b>This draft changes when the shot stops.</b> "
        "Stop conditions (volume / flow / pressure targets) differ from the base profile:"
        f"<ul>{items}</ul></div>"
    )


def _render_edits(edits: list[dict[str, Any]]) -> str:
    if not edits:
        return "<div class='card muted'>No profile edits yet — draft one from a review above.</div>"
    out = []
    for e in edits:
        phases = e["profile"].get("phases", [])
        warn = _stop_warning(e) if e["status"] == "draft" else ""
        notes = (
            f"<p class='muted' style='font-size:.88rem;margin:.4rem 0 0'>Your notes: "
            f"{html.escape(e['notes'])}</p>"
            if e.get("notes")
            else ""
        )
        if e["status"] == "draft":
            ack = (
                "<label class='ack'><input type='checkbox' name='ack' value='1' required> "
                "I've reviewed the stop-condition changes above</label>"
                if e.get("stop_changes")
                else ""
            )
            actions = (
                f"<form method='post' action='/edits/{e['id']}/push' class='inline'>{ack}"
                f"<button class='btn btn-sm' type='submit'>Approve &amp; push</button></form> "
                f"<form method='post' action='/edits/{e['id']}/discard' class='inline'>"
                f"<button class='btn btn-sm btn-ghost' type='submit'>Discard</button></form>"
            )
            refine = (
                f"<form method='post' action='/edits/{e['id']}/refine' style='margin-top:.7rem'>"
                "<textarea name='notes' rows='2' required placeholder='Tell Claude what to change "
                "before you approve — e.g. tasted bitter; keep the 9 bar peak; shorter preinfusion'>"
                "</textarea>"
                "<div class='row' style='margin-top:.4rem'>"
                "<button class='btn btn-sm btn-ghost' type='submit'>Redraft with these notes</button>"
                "<span class='muted' style='font-size:.82rem'>replaces this draft with a refined one</span>"
                "</div></form>"
            )
        else:
            refine = ""
            if e["status"] == "pushed":
                did = f" (id {html.escape(e['device_profile_id'])})" if e["device_profile_id"] else ""
                actions = (
                    f"<p class='muted' style='margin:.5rem 0 0'>Saved to machine as "
                    f"<code>{html.escape(e['label'])} [AI]</code>{did} — select it on the machine to use it.</p>"
                )
            elif e["status"] == "failed":
                actions = f"<p class='c-off' style='margin:.5rem 0 0'>Push failed: {html.escape(e['error'] or '')}</p>"
            else:
                actions = ""
        status_label = _EDIT_LABEL.get(e["status"], e["status"])
        out.append(
            f"""<div class="card">
              <div class="row" style="justify-content:space-between">
                <span>{_pill(status_label, _EDIT_PILL.get(e['status'], 'c-muted'))}
                  <b>Draft #{e['id']}</b>
                  <span class="muted">· based on {html.escape(e['base_profile_label'] or '—')} · {len(phases)} phase(s)</span></span>
              </div>
              <pre>{html.escape(e['change_summary'])}</pre>
              {notes}{warn}
              <div class="row">{actions}</div>
              {refine}
            </div>"""
        )
    return "".join(out)


def _fmt_shot_time(sh: dict[str, Any]) -> str:
    """Human-readable local date + time for a shot, e.g. 'Fri Jul 04, 14:32'.

    Uses the device capture time (unix seconds); falls back to the timestamp in
    the transformed JSON, then to '—' when the machine had no clock set.
    """
    ts = sh.get("captured_at") or sh.get("transformed", {}).get("timestamp")
    if not ts:
        return "time unknown"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%a %b %d, %H:%M")
    except (ValueError, OverflowError, OSError):
        return "time unknown"


def _fmt_qty(value: Any, suffix: str) -> str:
    """'49.3s' / '44.4g', or an em-dash when the value is missing."""
    if value is None or value == "":
        return "—"
    return f"{value}{suffix}"


def _render_shots(shots: list[dict[str, Any]]) -> str:
    if not shots:
        return "<div class='card muted'>No shots ingested yet.</div>"
    rows = []
    for sh in shots:
        t = sh["transformed"]
        rows.append(
            f"""<div class="card shot">
              <span class="meta"><b>Shot {html.escape(sh['id'])}</b>
                <span class="muted">· {html.escape(_fmt_shot_time(sh))}
                · {html.escape(str(t.get('profile_name') or '—'))}
                · {html.escape(_fmt_qty(t.get('duration_seconds'), 's'))}
                · {html.escape(_fmt_qty(t.get('final_weight_g'), 'g'))}</span></span>
              <form method="post" action="/analyze" class="inline">
                <input type="hidden" name="shot_id" value="{html.escape(sh['id'])}">
                <button class="btn btn-sm btn-ghost" type="submit">Review this shot</button>
              </form>
            </div>"""
        )
    return "".join(rows)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


_CONN_HINTS = (
    "connect call failed", "cannot connect", "no route to host", "device_unreachable",
    "name or service not known", "timed out", "timeout", "connection refused",
)


def _redirect_error(exc: Exception) -> RedirectResponse:
    msg = str(exc) or exc.__class__.__name__
    if any(h in msg.lower() for h in _CONN_HINTS):
        msg = "Can't reach the GaggiMate — it looks powered off or unreachable on the network."
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


_ICON_PATH = Path(__file__).parent / "icon.png"


@app.get("/icon.png", include_in_schema=False)
async def icon() -> FileResponse:
    """The crema mug icon (favicon + header logo). Cached hard — it never changes."""
    return FileResponse(
        _ICON_PATH, media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
async def index(error: Optional[str] = None, note: Optional[str] = None) -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        review = await db.latest_review(conn)
        shots = await db.recent_shots(conn, limit=_cfg.review_window)
        edits = await db.list_pending_edits(conn, limit=10)
        autoreview = await db.get_bool_setting(conn, "autoreview", _cfg.autoreview)
        grinder = (await db.get_setting(conn, "grinder")) or _cfg.grinder
    finally:
        await conn.close()
    auto_pill = _pill(f"auto-review {'on' if autoreview else 'off'}", "c-ok" if autoreview else "c-muted", dot=True)
    auto_toggle = (
        f"<form method='post' action='/autoreview' class='inline'>"
        f"<input type='hidden' name='on' value='{0 if autoreview else 1}'>"
        f"<button class='btn btn-sm btn-ghost' type='submit'>"
        f"Turn auto-review {'off' if autoreview else 'on'}</button></form>"
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
        <a class="brand" href="/"><img src="/icon.png" alt="" width="32" height="32">
          <h1>crema</h1></a>
        <span class="row">{status_pill}<span class="muted" style="font-size:.82rem">{html.escape(host)}</span></span>
      </header>
      <nav class="toc"><a href="#review">Latest review</a><a href="#edits">Profile edits</a><a href="#shots">Recent shots</a></nav>
      {banner}
      <div class="row" style="margin-top:.9rem">
        <form method="post" action="/review" class="inline">
          <button class="btn" type="submit" title="Pull new shots off the machine and review them">Review new shots</button></form>
        {auto_pill}{auto_toggle}
      </div>
      <form method="post" action="/grinder" class="row" style="margin-top:.6rem">
        <label class="muted" style="font-size:.88rem" for="grinder">Grinder</label>
        <input id="grinder" name="grinder" type="text" value="{html.escape(grinder)}"
          placeholder="e.g. Eureka Mignon Specialità, stepless — helps tailor grind advice"
          style="flex:1;min-width:14rem;font:inherit;color:var(--text);background:var(--surface-2);
          border:1px solid var(--border);border-radius:8px;padding:.35rem .6rem">
        <button class="btn btn-sm btn-ghost" type="submit">Save</button>
      </form>
      <h2 id="review">Latest review</h2>{_render_review(review, _profiles_in_shots(shots))}
      <h2 id="edits">Profile edits</h2>{_render_edits(edits)}
      <h2 id="shots">Recent shots</h2>{_render_shots(shots)}
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


@app.post("/grinder")
async def set_grinder(grinder: str = Form("")) -> RedirectResponse:
    """Save the grinder description used to tailor grind advice in reviews."""
    conn = await db.connect(_cfg.db_path)
    try:
        await db.set_setting(conn, "grinder", grinder.strip()[:300])
    finally:
        await conn.close()
    note = "Grinder saved — future reviews will phrase grind advice for it." if grinder.strip() else "Grinder cleared."
    return RedirectResponse("/?note=" + quote(note), status_code=303)


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
    review_id: int = Form(...),
    profile_id: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await draft_from_review(
            conn, _cfg, review_id, profile_id=profile_id or None,
            user_notes=(notes or "").strip() or None,
        )
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/refine")
async def refine_edit(edit_id: int, notes: str = Form(...)) -> RedirectResponse:
    """Redraft an awaiting-approval edit with the barista's notes; supersedes it."""
    conn = await db.connect(_cfg.db_path)
    try:
        edit = await db.get_pending_edit(conn, edit_id)
        if edit is None or edit["status"] != "draft" or not edit["review_id"]:
            return RedirectResponse(
                "/?error=" + quote(f"Draft #{edit_id} can't be refined."), status_code=303
            )
        await draft_from_review(
            conn, _cfg, edit["review_id"],
            user_notes=notes.strip() or None, refine_edit_id=edit_id,
        )
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/edits/{edit_id}/push")
async def approve_edit(edit_id: int, ack: Optional[str] = Form(None)) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        edit = await db.get_pending_edit(conn, edit_id)
        # A draft that changes stop conditions needs explicit acknowledgement —
        # enforced here too, not just by the checkbox in the form.
        if edit and edit.get("stop_changes") and ack != "1":
            return RedirectResponse(
                "/?error=" + quote(
                    f"Draft #{edit_id} changes the shot's stop conditions — tick the "
                    "acknowledgement box to confirm you've reviewed them before pushing."
                ),
                status_code=303,
            )
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
