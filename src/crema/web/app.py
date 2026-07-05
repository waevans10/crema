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
import hashlib
import hmac
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
from ..export import SHARE_TERMS, maybe_autoshare, record_autoshare_consent
from ..ingest import ingest_new_shots
from ..push import discard_edit, push_edit
from ..review import review_recent, review_shots
from ..starting import generate_starting_point

_cfg = CremaConfig()
_security = HTTPBasic(auto_error=False)

# Session cookie signing key: random, persisted in the settings table so logins
# survive service restarts. Loaded lazily on first use.
_SESSION_COOKIE = "crema_session"
_session_secret: Optional[bytes] = None


async def _get_session_secret() -> bytes:
    global _session_secret
    if _session_secret is None:
        conn = await db.connect(_cfg.db_path)
        try:
            stored = await db.get_setting(conn, "web_session_secret")
            if not stored:
                stored = secrets.token_hex(32)
                await db.set_setting(conn, "web_session_secret", stored)
        finally:
            await conn.close()
        _session_secret = bytes.fromhex(stored)
    return _session_secret


def _session_token(secret: bytes) -> str:
    """Deterministic HMAC over the configured credentials — rotating the web
    password (or the stored secret) invalidates existing sessions."""
    msg = f"crema-session-v1:{_cfg.web_user}:{_cfg.web_password}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _check_basic(credentials: Optional[HTTPBasicCredentials]) -> bool:
    return (
        credentials is not None
        and secrets.compare_digest(credentials.username, _cfg.web_user)
        and secrets.compare_digest(credentials.password, _cfg.web_password)
    )


# Paths reachable without auth (login page itself + the favicon it displays).
_PUBLIC_PATHS = {"/login", "/icon.png"}


async def require_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(_security),
) -> None:
    """Gate routes behind a login-form session cookie (password-manager friendly).

    HTTP Basic auth is still accepted in parallel so curl/scripts keep working.
    No web password configured = auth disabled (loopback use).
    """
    if not _cfg.web_password or request.url.path in _PUBLIC_PATHS:
        return
    cookie = request.cookies.get(_SESSION_COOKIE)
    if cookie and secrets.compare_digest(cookie, _session_token(await _get_session_secret())):
        return
    if _check_basic(credentials):
        return
    # Browsers get the login page; non-HTML clients get a plain 401.
    if "text/html" in (request.headers.get("accept") or ""):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"}
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


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
table.trend{width:100%;border-collapse:collapse;font-size:.88rem;font-variant-numeric:tabular-nums}
table.trend th{text-align:left;color:var(--muted);font-weight:700;font-size:.74rem;
  text-transform:uppercase;letter-spacing:.05em;padding:.3rem .5rem;border-bottom:1px solid var(--border)}
table.trend td{padding:.32rem .5rem;border-bottom:1px solid var(--border)}
table.trend tbody tr:last-child td{border-bottom:none}
.trend-scroll{overflow-x:auto}
.banner{border-left:4px solid;padding:.7rem 1rem;border-radius:10px;margin:.7rem 0;
  background:var(--surface)}
.banner.error{border-color:var(--off);color:var(--off)}
.banner.note{border-color:var(--muted);color:var(--muted)}
.banner.warn{border-color:var(--lo);color:var(--lo);background:var(--surface)}
.banner.warn ul{margin:.3rem 0 0;padding-left:1.2rem}
.shot{display:flex;align-items:center;justify-content:space-between;gap:.8rem}
.shot .meta{min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.45rem}
.chip{font:inherit;font-size:.78rem;font-weight:600;color:var(--text);
  background:var(--surface-2);border:1px solid var(--border);border-radius:999px;
  padding:.18rem .6rem;cursor:pointer;transition:border-color .12s}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip-label{font-size:.72rem;font-weight:700;letter-spacing:.04em;color:var(--muted);
  align-self:center;margin-right:.1rem;text-transform:uppercase}
.taste-guide svg{width:100%;height:auto;display:block;margin:.5rem 0 .2rem}
.taste-guide .zones{display:flex;gap:.6rem;font-size:.82rem;margin-top:.2rem}
.taste-guide .zones>div{flex:1;min-width:0}
.taste-guide .zones b{display:block;font-size:.78rem;letter-spacing:.03em;text-transform:uppercase}
.z-sour b{color:#c99a27}.z-ok b{color:var(--ok)}.z-bitter b{color:#a4653a}
@media (max-width:560px){.taste-guide .zones{flex-direction:column}}
.defs{display:flex;flex-direction:column;gap:.6rem;margin-top:.5rem;font-size:.84rem}
.def-group .def{margin:.18rem 0;color:var(--muted)}
.def-group .def b{color:var(--text)}
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
details.sec>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:.45rem;
  -webkit-user-select:none;user-select:none}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:'▾';color:var(--muted);font-size:.8rem;transition:transform .12s}
details.sec:not([open])>summary::before{transform:rotate(-90deg)}
details.sec>summary h2{margin:1.8rem 0 .6rem;display:inline-block}
details.sec:not([open])>summary h2{margin-bottom:.2rem}
details.sub{margin:.5rem 0 0}
details.sub>summary{list-style:none;cursor:pointer;color:var(--muted);font-size:.88rem;
  font-weight:600;-webkit-user-select:none;user-select:none}
details.sub>summary::-webkit-details-marker{display:none}
details.sub>summary::before{content:'▸ '}
details.sub[open]>summary::before{content:'▾ '}
details.sub>summary:hover{color:var(--accent)}
@media (max-width:640px){
  .wrap{padding:1rem .75rem 3rem}
  header{flex-direction:column;align-items:flex-start;gap:.4rem}
  h1{font-size:1.3rem}
  .card{padding:.85rem .95rem}
  .score{font-size:1.25rem;padding:.34rem .5rem}
  .kv{grid-template-columns:1fr;gap:.05rem}
  .kv .k{margin-top:.45rem}
  .shot{flex-direction:column;align-items:flex-start}
  .btn{padding:.55rem 1rem}
  pre{font-size:.82rem}
}
"""


# Appends a standard descriptor to the tasting-notes textarea in the same form.
_CHIP_JS = """
function tchip(b){var t=b.closest('form').querySelector('textarea');if(!t)return;
var w=b.getAttribute('data-w');t.value=t.value.trim()?t.value.replace(/[,\\s]+$/,'')+', '+w:w;t.focus();}
"""


def _page(body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>crema · shot report</title>"
        "<link rel='icon' type='image/png' href='/icon.png'>"
        "<link rel='apple-touch-icon' href='/icon.png'>"
        f"<style>{_CSS}</style><script>{_CHIP_JS}</script></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    )


def _pill(text: str, cls: str, dot: bool = False) -> str:
    d = "<span class='dot'></span>" if dot else ""
    return f"<span class='pill {cls}'>{d}{html.escape(text)}</span>"


async def _machine_status() -> tuple[bool, str]:
    """Fast reachability probe: a short TCP connect (not a request). 1.5s timeout."""
    if _cfg.machine == "gaggiuino":
        from urllib.parse import urlparse

        parsed = urlparse(_cfg.gaggiuino_url)
        host = parsed.hostname or "gaggiuino.local"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    else:
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
    if _cfg.machine != "gaggimate":
        return (
            "<p class='muted' style='margin-top:.8rem'>Profile drafting is GaggiMate-only "
            "for now — reviews, notes, and sharing all work on this machine.</p>"
        )
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
        "<details class='sub'><summary>Add notes for Claude (optional)</summary>"
        "<textarea name='notes' rows='2' style='margin-top:.4rem' placeholder='How it tasted, "
        "what you want (e.g. came out sour; keep preinfusion under 6s)'></textarea></details>"
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
      <details class="sub"><summary>Full reasoning</summary>
        <p class="muted" style="margin:.3rem 0 0">{html.escape(s.get('rationale', ''))}</p></details>
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
                "<details class='sub'><summary>Refine this draft with notes</summary>"
                f"<form method='post' action='/edits/{e['id']}/refine' style='margin-top:.4rem'>"
                "<textarea name='notes' rows='2' required placeholder='Tell Claude what to change "
                "before you approve — e.g. tasted bitter; keep the 9 bar peak; shorter preinfusion'>"
                "</textarea>"
                "<div class='row' style='margin-top:.4rem'>"
                "<button class='btn btn-sm btn-ghost' type='submit'>Redraft with these notes</button>"
                "<span class='muted' style='font-size:.82rem'>replaces this draft with a refined one</span>"
                "</div></form></details>"
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
        summary_pre = f"<pre>{html.escape(e['change_summary'])}</pre>"
        if e["status"] != "draft":
            # Historical edits: keep the page tight, changes one tap away.
            summary_pre = (
                f"<details class='sub'><summary>What changed</summary>{summary_pre}</details>"
            )
        out.append(
            f"""<div class="card">
              <div class="row" style="justify-content:space-between">
                <span>{_pill(status_label, _EDIT_PILL.get(e['status'], 'c-muted'))}
                  <b>Draft #{e['id']}</b>
                  <span class="muted">· based on {html.escape(e['base_profile_label'] or '—')} · {len(phases)} phase(s)</span></span>
              </div>
              {summary_pre}
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


# Standard tasting vocabulary, grouped by what it signals. Using these words
# consistently makes reviews sharper and shared bundles cleaner to study.
_TASTE_CHIPS: list[tuple[str, list[str]]] = [
    ("sour side", ["sour", "sharp", "thin", "salty", "quick finish"]),
    ("dialled in", ["sweet", "balanced", "syrupy", "long finish"]),
    ("bitter side", ["bitter", "harsh", "astringent", "drying", "hollow"]),
    ("strength", ["weak / watery", "too intense / muddy"]),
]

# What each word actually means in the mouth — shown in the guide's legend and
# as a tooltip on every chip, so newer palates can match word to sensation.
_TASTE_DEFS: dict[str, str] = {
    "sour": "acidic bite right at the front, like lemon juice or unripe fruit",
    "sharp": "aggressive first sip that pricks the tongue",
    "thin": "watery texture — no weight on the tongue",
    "salty": "faint saltiness at the edges of the tongue — a classic under-extraction tell",
    "quick finish": "the flavour vanishes seconds after you swallow",
    "sweet": "natural sweetness, like caramel or ripe fruit",
    "balanced": "no single note shouts — acidity, sweetness and bitterness in proportion",
    "syrupy": "thick, coating texture, like warm honey",
    "long finish": "the flavour keeps developing pleasantly after the sip",
    "bitter": "dark, roasty bite at the back of the tongue, like burnt toast",
    "harsh": "rough bitterness that bulldozes every other note",
    "astringent": "dries and puckers the mouth, like over-steeped black tea",
    "drying": "mouth feels parched right after swallowing",
    "hollow": "the aroma promises more than the taste delivers — an empty middle",
    "weak / watery": "tastes diluted overall — a strength (dose/ratio) issue, not extraction",
    "too intense / muddy": "so concentrated the notes blur together — a ratio issue",
}


def _taste_chips_html() -> str:
    groups = []
    for label, words in _TASTE_CHIPS:
        chips = "".join(
            f"<button type='button' class='chip' data-w='{html.escape(w)}' "
            f"title='{html.escape(_TASTE_DEFS.get(w, ''))}' "
            f"onclick='tchip(this)'>{html.escape(w)}</button>"
            for w in words
        )
        groups.append(f"<span class='chip-label'>{html.escape(label)}</span>{chips}")
    return f"<div class='chips'>{'&nbsp;&nbsp;'.join(groups)}</div>"


def _taste_legend_html() -> str:
    """Definition legend for the taste guide, grouped like the chips."""
    rows = []
    for label, words in _TASTE_CHIPS:
        defs = "".join(
            f"<div class='def'><b>{html.escape(w)}</b> — {html.escape(_TASTE_DEFS[w])}</div>"
            for w in words
            if w in _TASTE_DEFS
        )
        rows.append(f"<div class='def-group'><span class='chip-label'>{html.escape(label)}</span>{defs}</div>")
    return f"<div class='defs'>{''.join(rows)}</div>"


def _taste_hint(shot: dict[str, Any], review: Optional[dict[str, Any]]) -> str:
    """One line telling a newer taster what this shot will LIKELY taste like.

    Prefer the AI review of this very shot (real telemetry read); fall back to
    a plain duration heuristic when the shot hasn't been reviewed yet.
    """
    if review:
        diag = str(review.get("diagnosis") or "").strip()
        score = review.get("score")
        if diag:
            if len(diag) > 160:
                diag = diag[:157].rstrip() + "…"
            badge = f" (scored {score}/10)" if isinstance(score, int) else ""
            return f"AI read of this shot{badge}: {diag}"
    dur = shot.get("transformed", {}).get("duration_seconds")
    try:
        dur = float(dur)
    except (TypeError, ValueError):
        return ""
    if dur < 22:
        return (
            f"Telemetry hint: {dur:g}s is a fast shot — fast shots usually land on the "
            "sour side (sour, sharp, thin). Taste for that."
        )
    if dur > 40:
        return (
            f"Telemetry hint: {dur:g}s is a slow shot — slow shots usually land on the "
            "bitter side (bitter, harsh, drying). Taste for that."
        )
    return (
        f"Telemetry hint: {dur:g}s is in the classic window — judge sweetness and "
        "balance, and note whichever side it leans."
    )


# The extraction spectrum, as a theme-aware inline SVG: where a taste falls
# tells you which way to move the next shot.
_TASTE_GUIDE = """
<details class="card taste-guide"><summary>Espresso taste guide — which words mean what</summary>
  <svg viewBox="0 0 600 84" role="img" aria-label="Extraction spectrum from sour to bitter">
    <defs><linearGradient id="tg" x1="0" x2="1">
      <stop offset="0" stop-color="#c99a27"/><stop offset=".38" stop-color="#7d9958"/>
      <stop offset=".62" stop-color="#7d9958"/><stop offset="1" stop-color="#a4653a"/>
    </linearGradient></defs>
    <rect x="10" y="34" width="580" height="14" rx="7" fill="url(#tg)"/>
    <text x="10" y="22" fill="currentColor" font-size="13" font-weight="700">SOUR</text>
    <text x="300" y="22" fill="currentColor" font-size="13" font-weight="700" text-anchor="middle">SWEET SPOT</text>
    <text x="590" y="22" fill="currentColor" font-size="13" font-weight="700" text-anchor="end">BITTER</text>
    <text x="10" y="70" fill="currentColor" opacity=".65" font-size="11.5">under-extracted → grind finer</text>
    <text x="300" y="70" fill="currentColor" opacity=".65" font-size="11.5" text-anchor="middle">keep it here</text>
    <text x="590" y="70" fill="currentColor" opacity=".65" font-size="11.5" text-anchor="end">over-extracted → grind coarser</text>
  </svg>
  <div class="zones">
    <div class="z-sour"><b>Sour · under</b><span class="muted">sharp, thin, salty, lemony, finish
      vanishes fast. Water left too soon — grind finer (or hotter / longer).</span></div>
    <div class="z-ok"><b>Sweet · dialled in</b><span class="muted">sweetness, body, syrupy texture,
      a finish that lingers pleasantly. Change nothing.</span></div>
    <div class="z-bitter"><b>Bitter · over</b><span class="muted">harsh, astringent (dries the
      mouth), hollow. Water stayed too long — grind coarser (or cooler / shorter).</span></div>
  </div>
  <p class="muted" style="font-size:.82rem;margin:.6rem 0 0">Strength is a separate axis:
  <b>weak/watery</b> or <b>too intense/muddy</b> point at dose &amp; ratio, not extraction.
  Using these words in tasting notes helps the review connect your palate to the telemetry —
  the chips below each shot insert them for you.</p>
  <details class="sub" style="margin-top:.5rem"><summary>Word-by-word legend</summary>
  {legend}</details>
</details>"""


def _taste_guide_html() -> str:
    return _TASTE_GUIDE.replace("{legend}", _taste_legend_html())


def _render_shots(
    shots: list[dict[str, Any]],
    reviews_by_shot: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    if not shots:
        return "<div class='card muted'>No shots ingested yet.</div>"
    rows = [_taste_guide_html()]
    for sh in shots:
        t = sh["transformed"]
        notes = sh.get("tasting_notes") or ""
        shot_coffee = sh.get("coffee") or ""
        summary_bits = []
        if shot_coffee:
            summary_bits.append(f"Beans: {html.escape(shot_coffee)}")
        if notes:
            summary_bits.append(f"Tasting notes: {html.escape(notes)}")
        summary = " · ".join(summary_bits) or "Add beans / tasting notes for this shot"
        hint = _taste_hint(sh, (reviews_by_shot or {}).get(sh["id"]))
        hint_html = (
            f"<p class='muted' style='font-size:.82rem;margin:.45rem 0 0'>{html.escape(hint)}</p>"
            if hint
            else ""
        )
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
              <details class="sub"><summary>{summary}</summary>
                {hint_html}
                <form method="post" action="/shots/{html.escape(sh['id'])}/tasting-notes">
                  <input name="coffee" type="text" value="{html.escape(shot_coffee)}"
                    placeholder="Beans this shot was pulled with (blank = the session coffee)"
                    style="margin-top:.4rem;width:100%;font:inherit;color:var(--text);background:var(--surface-2);
                    border:1px solid var(--border);border-radius:8px;padding:.35rem .6rem">
                  {_taste_chips_html()}
                  <textarea name="notes" rows="2" style="margin-top:.4rem" placeholder="e.g. sour and thin — goes into the next review as taste feedback">{html.escape(notes)}</textarea>
                  <button class="btn btn-sm btn-ghost" type="submit">Save</button>
                </form>
              </details>
            </div>"""
        )
    return "".join(rows)


# --------------------------------------------------------------------------- #
# Beans: new-bean starting point + aging
# --------------------------------------------------------------------------- #


def _bean_age_days(roast_date: Optional[str]) -> Optional[int]:
    """Whole days since a bean's roast date (ISO YYYY-MM-DD), or None if unparseable."""
    if not roast_date:
        return None
    try:
        d = datetime.date.fromisoformat(roast_date.strip())
    except (ValueError, AttributeError):
        return None
    return (datetime.date.today() - d).days


def _aging_banner(bean: Optional[dict[str, Any]]) -> str:
    """Warn when the active bean is past its prime extraction window (roadmap P4)."""
    if not bean or not _cfg.bean_max_age_days:
        return ""
    age = _bean_age_days(bean.get("roast_date"))
    if age is None or age <= _cfg.bean_max_age_days:
        return ""
    return (
        f"<div class='banner warn'><b>{html.escape(bean['name'])} is {age} days past roast.</b> "
        "Past its prime — stale beans degas less and can run faster/flatter. "
        "Expect to grind a touch finer, and taste for a hollow or muted cup.</div>"
    )


def _select(name: str, options: list[str], selected: str = "", blank: str = "") -> str:
    """A restricted-vocabulary <select> — keeps bean data unified for matching."""
    opts = []
    if blank:
        opts.append(f"<option value=''>{html.escape(blank)}</option>")
    for o in options:
        sel = " selected" if o == selected else ""
        opts.append(f"<option value='{html.escape(o)}'{sel}>{html.escape(o)}</option>")
    return f"<select name='{name}'>{''.join(opts)}</select>"


def _new_bean_card(active_bean: Optional[dict[str, Any]], has_grinder: bool) -> str:
    """The 'start a new bean' panel: a structured form → a staged starting profile."""
    active = ""
    if active_bean:
        age = _bean_age_days(active_bean.get("roast_date"))
        age_txt = f" · {age}d off roast" if age is not None else ""
        active = (
            f"<p class='muted' style='font-size:.88rem;margin:0 0 .5rem'>In the hopper: "
            f"<b>{html.escape(active_bean['name'])}</b> · {html.escape(active_bean['roast_level'])} roast"
            f"{html.escape(age_txt)}</p>"
        )
    grinder_hint = (
        ""
        if has_grinder
        else "<p class='muted' style='font-size:.82rem;margin:.4rem 0 0'>Tip: set your "
        "grinder in Settings so the grind is given in its own units.</p>"
    )
    fld = ("style='font:inherit;color:var(--text);background:var(--surface-2);"
           "border:1px solid var(--border);border-radius:8px;padding:.4rem .6rem'")
    return f"""<div class="card">
      {active}
      <form method="post" action="/beans/start">
        <div class="row" style="gap:.5rem">
          <input name="name" type="text" required placeholder="Bean / origin, e.g. Colombian Huila"
            {fld} style="flex:1;min-width:12rem;font:inherit;color:var(--text);background:var(--surface-2);
            border:1px solid var(--border);border-radius:8px;padding:.4rem .6rem">
          {_select("roast_level", db.ROAST_LEVELS, selected="light")}
          {_select("process", db.PROCESSES, blank="process (optional)")}
        </div>
        <div class="row" style="gap:.5rem;margin-top:.5rem">
          <label class="muted" style="font-size:.85rem">Roasted <input name="roast_date" type="date" {fld}></label>
          <label class="muted" style="font-size:.85rem">Dose <input name="dose" type="number" step="0.1"
            min="5" max="30" placeholder="18" {fld} style="width:5.5rem;font:inherit;color:var(--text);
            background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:.4rem .6rem"> g</label>
          <button class="btn" type="submit" title="Generate a starting grind + profile from your similar shots">
            Generate starting shot</button>
        </div>
      </form>
      <p class="muted" style="font-size:.82rem;margin:.55rem 0 0">Uses your most similar past shots when it
      can, roast-level first principles when it can't. The result is staged below for you to approve — nothing
      is pushed automatically.</p>
      {grinder_hint}
    </div>"""


# --------------------------------------------------------------------------- #
# Trends: server-rendered SVG (no charting library — light on the Pi)
# --------------------------------------------------------------------------- #


def _rolling_avg(values: list[float], window: int = 7) -> list[float]:
    """Trailing average over the last `window` points (partial at the start)."""
    out: list[float] = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1) : i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _score_chart_svg(scored: list[dict[str, Any]]) -> str:
    """Score-over-time line + 7-shot rolling average + bean-change markers.

    `scored` is oldest→newest and every row has an integer `score`. Pure SVG with
    theme-aware CSS-variable colors; each point links to nothing heavier than the
    shots list. No JS.
    """
    W, H = 600, 220
    pad_l, pad_r, pad_t, pad_b = 34, 12, 12, 26
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    n = len(scored)
    x_step = plot_w / max(n - 1, 1)

    def x(i: int) -> float:
        return pad_l + i * x_step

    def y(score: float) -> float:
        # Score 1..10 → bottom..top of the plot.
        return pad_t + plot_h - ((score - 1) / 9.0) * plot_h

    # Poor / okay / good bands (score ≤3, 4–6, ≥7).
    bands = (
        f"<rect x='{pad_l}' y='{y(10)}' width='{plot_w}' height='{y(7) - y(10)}' fill='var(--hi)' opacity='.10'/>"
        f"<rect x='{pad_l}' y='{y(7)}' width='{plot_w}' height='{y(4) - y(7)}' fill='var(--lo)' opacity='.10'/>"
        f"<rect x='{pad_l}' y='{y(4)}' width='{plot_w}' height='{y(1) - y(4)}' fill='var(--off)' opacity='.10'/>"
    )
    # Y gridline labels at 2/4/6/8/10.
    grid = "".join(
        f"<text x='{pad_l - 6}' y='{y(s) + 3:.1f}' text-anchor='end' font-size='10' "
        f"fill='currentColor' opacity='.5'>{s}</text>"
        for s in (2, 4, 6, 8, 10)
    )
    scores = [float(r["score"]) for r in scored]
    pts = " ".join(f"{x(i):.1f},{y(s):.1f}" for i, s in enumerate(scores))
    dots = "".join(
        f"<circle cx='{x(i):.1f}' cy='{y(s):.1f}' r='3' fill='var(--accent)'>"
        f"<title>shot {html.escape(scored[i]['id'])}: {int(s)}/10</title></circle>"
        for i, s in enumerate(scores)
    )
    avg = _rolling_avg(scores)
    avg_pts = " ".join(f"{x(i):.1f},{y(a):.1f}" for i, a in enumerate(avg))
    # Bean-change markers: vertical lines where the beans differ from the point before.
    markers = ""
    for i in range(1, n):
        prev, cur = scored[i - 1].get("coffee"), scored[i].get("coffee")
        if cur and cur != prev:
            mx = x(i) - x_step / 2
            markers += (
                f"<line x1='{mx:.1f}' y1='{pad_t}' x2='{mx:.1f}' y2='{pad_t + plot_h}' "
                f"stroke='var(--mid)' stroke-width='1' stroke-dasharray='3 3' opacity='.6'>"
                f"<title>beans changed → {html.escape(str(cur))}</title></line>"
            )
    return f"""<svg viewBox="0 0 {W} {H}" role="img" aria-label="Shot score over time"
        style="width:100%;height:auto;display:block">
      {bands}{grid}{markers}
      <polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>
      <polyline points="{avg_pts}" fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-dasharray="5 4" opacity=".55"/>
      {dots}
    </svg>"""


def _trends_table(rows: list[dict[str, Any]]) -> str:
    """Newest-first table of recent shots: date, score, yield, time, beans."""
    body = []
    for r in reversed(rows):  # rows are oldest→newest; show newest first
        score = r["score"]
        score_cell = f"{score}/10" if isinstance(score, int) else "—"
        body.append(
            "<tr>"
            f"<td>{html.escape(r['id'])}</td>"
            f"<td>{html.escape(_fmt_ts(r.get('captured_at')) or '—')}</td>"
            f"<td style='text-align:right'>{score_cell}</td>"
            f"<td style='text-align:right'>{html.escape(_fmt_qty(r.get('yield_g'), 'g'))}</td>"
            f"<td style='text-align:right'>{html.escape(_fmt_qty(r.get('duration_s'), 's'))}</td>"
            f"<td class='muted'>{html.escape(str(r.get('coffee') or '—'))}</td>"
            "</tr>"
        )
    return (
        "<div class='card trend-scroll'><table class='trend'>"
        "<thead><tr><th>Shot</th><th>When</th><th>Score</th>"
        "<th>Yield</th><th>Time</th><th>Beans</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _render_trends(rows: list[dict[str, Any]]) -> str:
    scored = [r for r in rows if isinstance(r["score"], int)]
    if len(scored) < 3:
        chart = (
            "<div class='card muted'>Pull and review a few more shots to see the trend — "
            f"{len(scored)} scored so far, need at least 3.</div>"
        )
    else:
        chart = (
            "<div class='card'>"
            + _score_chart_svg(scored)
            + "<div class='row muted' style='justify-content:center;gap:1.2rem;font-size:.8rem;"
            "margin-top:.5rem'>"
            "<span class='c-accent'>● score</span>"
            "<span>– – 7-shot average</span>"
            "<span class='c-mid'>┆ beans changed</span></div></div>"
        )
    table = _trends_table(rows) if rows else "<div class='card muted'>No shots yet.</div>"
    return f"""
      <header>
        <a class="brand" href="/"><img src="/icon.png" alt="" width="32" height="32"><h1>crema · trends</h1></a>
        <a class="btn btn-sm btn-ghost" href="/">← report</a>
      </header>
      <h2>Shot score over time</h2>
      {chart}
      <h2>Recent shots</h2>
      {table}
    """


@app.get("/trends", response_class=HTMLResponse)
async def trends() -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        rows = await db.score_history(conn, limit=40)
    finally:
        await conn.close()
    return _page(_render_trends(rows))


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


def _login_page(error: str = "") -> str:
    banner = f"<div class='banner error'>{html.escape(error)}</div>" if error else ""
    return _page(f"""
      <header style="justify-content:center;margin-top:3rem">
        <span class="brand"><img src="/icon.png" alt="" width="32" height="32"><h1>crema</h1></span>
      </header>
      {banner}
      <div class="card" style="max-width:22rem;margin:1rem auto">
        <form method="post" action="/login">
          <label class="muted" for="u" style="font-size:.88rem">Username</label>
          <input id="u" name="username" type="text" autocomplete="username" required
            style="width:100%;font:inherit;color:var(--text);background:var(--surface-2);
            border:1px solid var(--border);border-radius:8px;padding:.5rem .6rem;margin:.2rem 0 .8rem">
          <label class="muted" for="p" style="font-size:.88rem">Password</label>
          <input id="p" name="password" type="password" autocomplete="current-password" required
            style="width:100%;font:inherit;color:var(--text);background:var(--surface-2);
            border:1px solid var(--border);border-radius:8px;padding:.5rem .6rem;margin:.2rem 0 1rem">
          <button class="btn" type="submit" style="width:100%">Sign in</button>
        </form>
      </div>
    """)


@app.get("/login", response_class=HTMLResponse)
async def login_form() -> str:
    if not _cfg.web_password:
        # Auth disabled — nothing to log in to.
        raise HTTPException(status_code=307, headers={"Location": "/"})
    return _login_page()


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if not _cfg.web_password:
        return RedirectResponse("/", status_code=303)
    if not (
        secrets.compare_digest(username, _cfg.web_user)
        and secrets.compare_digest(password, _cfg.web_password)
    ):
        return HTMLResponse(_login_page("Wrong username or password."), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        _SESSION_COOKIE,
        _session_token(await _get_session_secret()),
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login" if _cfg.web_password else "/", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(error: Optional[str] = None, note: Optional[str] = None) -> str:
    conn = await db.connect(_cfg.db_path)
    try:
        review = await db.latest_review(conn)
        shots = await db.recent_shots(conn, limit=_cfg.review_window)
        edits = await db.list_pending_edits(conn, limit=10)
        autoreview = await db.get_bool_setting(conn, "autoreview", _cfg.autoreview)
        autoshare = await db.get_bool_setting(conn, "autoshare", False)
        grinder = (await db.get_setting(conn, "grinder")) or _cfg.grinder
        coffee = (await db.get_setting(conn, "coffee")) or _cfg.coffee
        active_bean = await db.active_bean(conn)
        reviews_by_shot = await db.latest_reviews_for_shots(conn, [s["id"] for s in shots])
    finally:
        await conn.close()
    auto_pill = _pill(f"auto-review {'on' if autoreview else 'off'}", "c-ok" if autoreview else "c-muted", dot=True)
    auto_toggle = (
        f"<form method='post' action='/autoreview' class='inline'>"
        f"<input type='hidden' name='on' value='{0 if autoreview else 1}'>"
        f"<button class='btn btn-sm btn-ghost' type='submit'>"
        f"Turn auto-review {'off' if autoreview else 'on'}</button></form>"
    )
    share_pill = _pill(f"auto-share {'on' if autoshare else 'off'}", "c-ok" if autoshare else "c-muted", dot=True)
    if autoshare:
        share_toggle = (
            "<form method='post' action='/autoshare' class='inline'>"
            "<input type='hidden' name='on' value='0'>"
            "<button class='btn btn-sm btn-ghost' type='submit'>Turn auto-share off</button></form>"
        )
    else:
        share_toggle = (
            "<details class='sub inline'><summary>Opt in to the community shot pool</summary>"
            f"<pre style='margin:.5rem 0'>{html.escape(SHARE_TERMS)}</pre>"
            "<form method='post' action='/autoshare' class='inline'>"
            "<input type='hidden' name='on' value='1'>"
            "<button class='btn btn-sm' type='submit'>I agree — turn auto-share on</button></form>"
            "</details>"
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
        <span class="row">{status_pill}<span class="muted" style="font-size:.82rem">{html.escape(host)}</span>
          {"<form method='post' action='/logout' class='inline'><button class='btn btn-sm btn-ghost' type='submit'>Sign out</button></form>" if _cfg.web_password else ""}</span>
      </header>
      <nav class="toc"><a href="#newbean">New bean</a><a href="#review">Latest review</a>
        <a href="#edits">Profile edits</a><a href="#shots">Recent shots</a><a href="/trends">Trends</a></nav>
      {banner}
      {_aging_banner(active_bean)}
      <div class="row" style="margin-top:.9rem;gap:.5rem .8rem">
        <form method="post" action="/review" class="inline">
          <button class="btn" type="submit" title="Pull new shots off the machine and review them">Review new shots</button></form>
        {auto_pill}{auto_toggle}
        <span style="flex-basis:100%;height:0"></span>
        {share_pill}{share_toggle}
      </div>
      <details class="sec" open id="newbean"><summary><h2>Start a new bean</h2></summary>
        {_new_bean_card(active_bean, bool(grinder))}</details>
      <details class="sec" open id="review"><summary><h2>Latest review</h2></summary>
        {_render_review(review, _profiles_in_shots(shots))}</details>
      <details class="sec" open id="edits"><summary><h2>Profile edits</h2></summary>
        {_render_edits(edits)}</details>
      <details class="sec" open id="shots"><summary><h2>Recent shots</h2></summary>
        {_render_shots(shots, reviews_by_shot)}</details>
      <details class="sec" id="settings"><summary><h2>Settings — grinder &amp; coffee</h2></summary>
        <div class="card">
          <form method="post" action="/grinder" class="row">
            <label class="muted" style="font-size:.88rem;min-width:4rem" for="grinder">Grinder</label>
            <input id="grinder" name="grinder" type="text" value="{html.escape(grinder)}"
              placeholder="e.g. Eureka Mignon Specialità, stepless — helps tailor grind advice"
              style="flex:1;min-width:12rem;font:inherit;color:var(--text);background:var(--surface-2);
              border:1px solid var(--border);border-radius:8px;padding:.35rem .6rem">
            <button class="btn btn-sm btn-ghost" type="submit">Save</button>
          </form>
          <form method="post" action="/coffee" class="row" style="margin-top:.6rem">
            <label class="muted" style="font-size:.88rem;min-width:4rem" for="coffee">Coffee</label>
            <input id="coffee" name="coffee" type="text" value="{html.escape(coffee)}"
              placeholder="Free-text beans — or use ‘Start a new bean’ above to set them from a starting shot"
              style="flex:1;min-width:12rem;font:inherit;color:var(--text);background:var(--surface-2);
              border:1px solid var(--border);border-radius:8px;padding:.35rem .6rem">
            <button class="btn btn-sm btn-ghost" type="submit">Save</button>
          </form>
        </div></details>
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
        shared = await maybe_autoshare(conn, _cfg)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    if shared:
        return RedirectResponse("/?note=" + quote(shared.capitalize()), status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/autoreview")
async def toggle_autoreview(on: str = Form(...)) -> RedirectResponse:
    conn = await db.connect(_cfg.db_path)
    try:
        await db.set_setting(conn, "autoreview", "1" if on == "1" else "0")
    finally:
        await conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/autoshare")
async def toggle_autoshare(on: str = Form(...)) -> RedirectResponse:
    """Opt in to (terms shown beside the button) or out of pool auto-sharing."""
    conn = await db.connect(_cfg.db_path)
    try:
        if on == "1":
            await record_autoshare_consent(conn)
            note = "Auto-share is ON — a snapshot uploads after each review. Turn it off here any time."
        else:
            await db.set_setting(conn, "autoshare", "0")
            note = "Auto-share is OFF. Nothing is shared unless you run `crema share` yourself."
    finally:
        await conn.close()
    return RedirectResponse("/?note=" + quote(note), status_code=303)


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


@app.post("/coffee")
async def set_coffee(coffee: str = Form("")) -> RedirectResponse:
    """Save the coffee description used to ground review advice in the beans."""
    conn = await db.connect(_cfg.db_path)
    try:
        await db.set_setting(conn, "coffee", coffee.strip()[:300])
    finally:
        await conn.close()
    note = "Coffee saved — future reviews will tailor advice to these beans." if coffee.strip() else "Coffee cleared."
    return RedirectResponse("/?note=" + quote(note), status_code=303)


@app.post("/beans/start")
async def start_new_bean(
    name: str = Form(...),
    roast_level: str = Form(...),
    process: str = Form(""),
    roast_date: str = Form(""),
    dose: str = Form(""),
) -> RedirectResponse:
    """Add a bean, make it active, and stage a starting-point profile for approval."""
    name = name.strip()[:120]
    if not name:
        return RedirectResponse("/?error=" + quote("Give the bean a name."), status_code=303)
    if roast_level not in db.ROAST_LEVELS:
        return RedirectResponse("/?error=" + quote("Pick a roast level."), status_code=303)
    dose_val: Optional[float] = None
    if dose.strip():
        try:
            dose_val = float(dose)
        except ValueError:
            dose_val = None
    conn = await db.connect(_cfg.db_path)
    try:
        bean_id = await db.insert_bean(
            conn,
            name=name,
            roast_level=roast_level,
            process=process.strip() or None,
            roast_date=roast_date.strip() or None,
        )
        await db.set_active_bean(conn, bean_id)
        bean = await db.get_bean(conn, bean_id)
        result = await generate_starting_point(conn, _cfg, bean, dose_target=dose_val)
    except Exception as e:  # noqa: BLE001
        return _redirect_error(e)
    finally:
        await conn.close()
    edit = result["edit"]
    n_sim = len(result["similar"])
    basis = f"from {n_sim} similar past shot(s)" if n_sim else "from roast-level first principles"
    note = (
        f"Starting point for {name} staged as Draft #{edit['id']} ({basis}). "
        "Review it under Profile edits, then Approve & push when you're happy."
    )
    return RedirectResponse("/?note=" + quote(note) + "#edits", status_code=303)


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


@app.post("/shots/{shot_id}/tasting-notes")
async def save_tasting_notes(
    shot_id: str, notes: str = Form(""), coffee: str = Form("")
) -> RedirectResponse:
    """Save the barista's tasting notes and/or beans on a shot for future reviews."""
    conn = await db.connect(_cfg.db_path)
    try:
        found = await db.set_shot_tasting_notes(conn, shot_id, notes.strip()[:500] or None)
        if found:
            await db.set_shot_coffee(conn, shot_id, coffee.strip()[:300] or None)
    finally:
        await conn.close()
    if not found:
        return RedirectResponse("/?error=" + quote(f"Shot {shot_id} not found."), status_code=303)
    msg = (
        f"Shot {shot_id} updated — the next review will take it into account."
        if notes.strip() or coffee.strip()
        else f"Beans and tasting notes cleared for shot {shot_id}."
    )
    return RedirectResponse("/?note=" + quote(msg), status_code=303)


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
