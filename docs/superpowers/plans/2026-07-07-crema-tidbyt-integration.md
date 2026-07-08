# crema Tidbyt integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in Tidbyt integration that shows the latest reviewed shot's 1–10 score and profile name on a Tidbyt display, pushed the moment a review is stored.

**Architecture:** A new `src/crema/tidbyt.py` renders a 64×32 WebP frame with Pillow and POSTs it to Tidbyt's HTTP push API. It rides crema's existing review-completion event next to the Discord notifier (`src/crema/review.py` → `notify.notify_review`), is a no-op unless `CREMA_TIDBYT_*` env vars are set, and swallows all errors so a display problem never breaks a review.

**Tech Stack:** Python 3.13, `pydantic-settings` (config), Pillow (render, WebP), `aiohttp` (already a dep, async push), pytest + pytest-asyncio (`asyncio_mode=auto`).

## Global Constraints

- Feature is **off by default**: no `CREMA_TIDBYT_API_TOKEN` + `CREMA_TIDBYT_DEVICE_ID` ⇒ complete no-op, zero behavior change.
- **Best-effort, never raises into the review path**: every failure logs at `warning` and is swallowed — same contract as `src/crema/notify.py`.
- **No `pixlet`, no `.star`, no Go toolchain** — target hardware is a 32-bit armv7 Pi (Pi Zero 2 W); pure-Python render only.
- Tests are **offline**: no live device, no API token, no network — patch the HTTP call. Match the existing style in `tests/test_smoke.py` (plain functions, `asyncio_mode=auto`).
- Score color thresholds mirror `notify.py`: red `<4`, amber `<7`, green `≥7`.
- Push API: `POST https://api.tidbyt.com/v0/devices/{device_id}/push`, header `Authorization: Bearer {token}`, body `{"image": "<base64 webp>", "installationID": "<id>", "background": false}`.
- Run tests with: `uv run pytest`.

---

### Task 1: `render_frame` — Pillow WebP renderer

**Files:**
- Modify: `pyproject.toml` (add `Pillow` to `dependencies`)
- Create: `src/crema/tidbyt.py`
- Test: `tests/test_tidbyt.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `_score_color(score: int | None) -> tuple[int, int, int]`
  - `render_frame(score: int | None, profile: str | None, bean: str | None = None, stale: bool = False) -> bytes` — returns WebP bytes for a 64×32 frame.

- [ ] **Step 1: Add Pillow dependency**

In `pyproject.toml`, under `[project]` `dependencies = [ ... ]`, add the line (keep the list alphabetical-ish, next to `aiohttp`):

```toml
    "pillow>=11.0",
```

Then sync:

```bash
uv sync
```

Expected: resolves and installs Pillow without error.

- [ ] **Step 2: Write the failing test**

Create `tests/test_tidbyt.py`:

```python
"""Offline tests for the Tidbyt integration — no device, no network."""

from __future__ import annotations

from crema import tidbyt


def test_score_color_bands():
    assert tidbyt._score_color(2) == (192, 57, 43)    # red  (<4)
    assert tidbyt._score_color(5) == (194, 135, 26)   # amber (<7)
    assert tidbyt._score_color(9) == (63, 143, 67)    # green (>=7)
    assert tidbyt._score_color(None) == (120, 120, 120)  # neutral


def test_render_frame_returns_webp_bytes():
    data = tidbyt.render_frame(8, "Lavazza Super Crema 18g Latte [AI]", bean="Lavazza")
    assert isinstance(data, bytes) and len(data) > 0
    # RIFF....WEBP container signature
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def test_render_frame_tolerates_missing_fields():
    # No score, no profile, stale flag — must not raise and still produce a frame.
    data = tidbyt.render_frame(None, None, stale=True)
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_tidbyt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'crema.tidbyt'`.

- [ ] **Step 4: Write minimal implementation**

Create `src/crema/tidbyt.py`:

```python
"""Tidbyt integration: render the latest reviewed shot and push it to a Tidbyt.

Opt-in and best-effort — a no-op unless CREMA_TIDBYT_API_TOKEN and
CREMA_TIDBYT_DEVICE_ID are set, and any failure is logged and swallowed so a
display problem never breaks a review (same contract as notify.py). Pure-Python
render (Pillow) so it runs on the 32-bit armv7 Pi crema targets — no pixlet.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from .config import CremaConfig

_log = logging.getLogger(__name__)

WIDTH, HEIGHT = 64, 32


def _score_color(score: Optional[int]) -> tuple[int, int, int]:
    if score is None:
        return (120, 120, 120)  # neutral grey
    if score < 4:
        return (192, 57, 43)    # red
    if score < 7:
        return (194, 135, 26)   # amber
    return (63, 143, 67)        # green


def _wrap(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, text: str,
          max_w: int, max_lines: int) -> list[str]:
    """Greedy word-wrap `text` to `max_w` pixels, ellipsizing past `max_lines`."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines:
        # Ellipsize the last line if we truncated or it still overflows.
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_w:
            lines[-1] = lines[-1][:-1]
        if len(" ".join(lines)) < len(text):
            lines[-1] = (lines[-1] + "…") if lines[-1] else "…"
    return lines


def render_frame(score: Optional[int], profile: Optional[str],
                 bean: Optional[str] = None, stale: bool = False) -> bytes:
    """Render a 64x32 WebP: big score on the left, profile name wrapped on the right."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    big = ImageFont.load_default(size=18)   # Pillow >= 10.1 supports size=
    small = ImageFont.load_default(size=8)

    label = "–" if score is None else str(int(score))
    draw.text((3, 5), label, fill=_score_color(score), font=big)
    draw.text((3, 24), "/10", fill=(90, 90, 90), font=small)

    name = (profile or "?").strip()
    for i, line in enumerate(_wrap(draw, small, name, max_w=WIDTH - 26, max_lines=3)):
        draw.text((25, 2 + i * 10), line, fill=(210, 210, 210), font=small)

    if stale:
        draw.point((WIDTH - 1, 0), fill=(70, 70, 70))

    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    return buf.getvalue()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_tidbyt.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/crema/tidbyt.py tests/test_tidbyt.py
git commit -m "feat(tidbyt): render latest-shot frame with Pillow"
```

---

### Task 2: Config keys + `push_review` (no-op guard + HTTP push)

**Files:**
- Modify: `src/crema/config.py` (add three optional fields near `discord_webhook_url`, ~line 99)
- Modify: `src/crema/tidbyt.py` (add `push_review`)
- Modify: `.env.example` (document the new keys)
- Test: `tests/test_tidbyt.py`

**Interfaces:**
- Consumes: `render_frame(...)` (Task 1); `CremaConfig` fields `tidbyt_api_token`, `tidbyt_device_id`, `tidbyt_installation_id`.
- Produces: `async def push_review(config: CremaConfig, review: dict[str, Any]) -> None`.

- [ ] **Step 1: Add config fields**

In `src/crema/config.py`, immediately after the `discord_webhook_url: str = ""` field, add:

```python
    # Optional Tidbyt push. When BOTH the api token and device id are set, crema
    # renders each reviewed shot's score + profile name and pushes it to your
    # Tidbyt. Empty = disabled. Find both in the Tidbyt app / api.tidbyt.com.
    tidbyt_api_token: str = ""
    tidbyt_device_id: str = ""
    # Which app slot on the device to replace on each push (re-pushing the same id
    # updates the one slot instead of piling up).
    tidbyt_installation_id: str = "crema"
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_tidbyt.py`:

```python
import asyncio

from crema.config import CremaConfig


def _review(score=8, profile="Lavazza 18g [AI]", bean="Lavazza"):
    return {"shot_id": "000094", "profile_name": profile, "bean": bean,
            "suggestions": {"score": score}}


def test_push_review_noop_when_unconfigured(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="", tidbyt_device_id="")
    calls = []

    async def fake_post(self, url, **kw):  # pragma: no cover - must never run
        calls.append(url)

    monkeypatch.setattr("aiohttp.ClientSession.post", fake_post)
    asyncio.run(tidbyt.push_review(cfg, _review()))
    assert calls == []  # HTTP never attempted


def test_push_review_posts_expected_request(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="tok123", tidbyt_device_id="devABC",
                      tidbyt_installation_id="crema")
    captured = {}

    class _Resp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def text(self): return "ok"

    def fake_post(self, url, headers=None, json=None, **kw):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("aiohttp.ClientSession.post", fake_post)
    asyncio.run(tidbyt.push_review(cfg, _review()))

    assert captured["url"] == "https://api.tidbyt.com/v0/devices/devABC/push"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["json"]["installationID"] == "crema"
    assert captured["json"]["background"] is False
    assert isinstance(captured["json"]["image"], str) and captured["json"]["image"]


def test_push_review_swallows_errors(monkeypatch):
    cfg = CremaConfig(tidbyt_api_token="tok", tidbyt_device_id="dev")

    def boom(self, *a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("aiohttp.ClientSession.post", boom)
    # Must not raise.
    asyncio.run(tidbyt.push_review(cfg, _review()))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_tidbyt.py -k push_review -v`
Expected: FAIL — `AttributeError: module 'crema.tidbyt' has no attribute 'push_review'`.

- [ ] **Step 4: Implement `push_review`**

Add to `src/crema/tidbyt.py` (imports at top: add `import base64` and `import aiohttp`):

```python
_PUSH_URL = "https://api.tidbyt.com/v0/devices/{device_id}/push"


async def push_review(config: CremaConfig, review: dict[str, Any]) -> None:
    """Render a reviewed shot and push it to the Tidbyt. No-op if unconfigured."""
    token = config.tidbyt_api_token
    device = config.tidbyt_device_id
    if not token or not device:
        return

    try:
        s = review.get("suggestions") or {}
        raw = s.get("score")
        score = max(1, min(10, int(raw))) if isinstance(raw, (int, float)) else None
        frame = render_frame(
            score,
            review.get("profile_name"),
            bean=review.get("bean"),
        )
        payload = {
            "image": base64.b64encode(frame).decode("ascii"),
            "installationID": config.tidbyt_installation_id,
            "background": False,
        }
        url = _PUSH_URL.format(device_id=device)
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    _log.warning("Tidbyt push failed (%s): %s", resp.status, body[:200])
    except Exception:  # noqa: BLE001 — a display problem must never break a review
        _log.warning("Tidbyt push errored; skipping.", exc_info=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_tidbyt.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 6: Document env keys**

In `.env.example`, add near the Discord section:

```bash
# --- Tidbyt (optional) --------------------------------------------------------
# Show each reviewed shot's score + profile name on a Tidbyt display. Off unless
# BOTH of these are set. Find them in the Tidbyt app (device settings) or at
# api.tidbyt.com. Free — no Claude/API cost; a local render + one push.
# CREMA_TIDBYT_API_TOKEN=
# CREMA_TIDBYT_DEVICE_ID=
# CREMA_TIDBYT_INSTALLATION_ID=crema
```

- [ ] **Step 7: Commit**

```bash
git add src/crema/config.py src/crema/tidbyt.py tests/test_tidbyt.py .env.example
git commit -m "feat(tidbyt): push_review with config guard + Tidbyt push API"
```

---

### Task 3: Wire into the review path + user docs

**Files:**
- Modify: `src/crema/review.py` (enrich `stored`, call `push_review` after `notify_review`, ~lines 88–101)
- Modify: `deploy/PI_SETUP.md` (setup note)
- Modify: `README.md` (opt-in feature section)
- Test: `tests/test_tidbyt.py`

**Interfaces:**
- Consumes: `push_review(config, review)` (Task 2); `shots[0]["transformed"]["profile_name"]`, `shots[0]["coffee"]`.
- Produces: `stored` dict now also carries `"profile_name"` and `"bean"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tidbyt.py` a test that the review payload is enriched. Add near the top import: `from crema import review as review_mod`. Then:

```python
def test_stored_enrichment_shape():
    # Guard the contract push_review depends on: review.py must add these keys.
    # (Unit-level: build the dict the way review.py does and assert push reads it.)
    shot = {"id": "000094", "transformed": {"profile_name": "Lavazza [AI]"},
            "coffee": "Lavazza Super Crema"}
    stored = {"id": 1, "shot_id": shot["id"], "suggestions": {"score": 7}}
    stored["profile_name"] = shot["transformed"].get("profile_name")
    stored["bean"] = shot.get("coffee")
    assert stored["profile_name"] == "Lavazza [AI]"
    assert stored["bean"] == "Lavazza Super Crema"
```

- [ ] **Step 2: Run it to confirm it passes as a spec of the shape**

Run: `uv run pytest tests/test_tidbyt.py::test_stored_enrichment_shape -v`
Expected: PASS (this pins the shape review.py must produce; Step 3 makes review.py match it).

- [ ] **Step 3: Enrich `stored` and call `push_review` in `review.py`**

In `src/crema/review.py`, change the import line `from . import db, notify` to:

```python
from . import db, notify, tidbyt
```

Then replace the `stored = { ... }` block and the notify call (currently ending with `await notify.notify_review(config, stored)`) so `stored` gains the two fields and both channels fire:

```python
    stored = {
        "id": review_id,
        "shot_id": newest_shot_id,
        "model": config.review_model,
        "suggestions": suggestions,
        # Enriched for notifiers/displays: the reviewed shot's profile + beans.
        "profile_name": shots[0]["transformed"].get("profile_name"),
        "bean": shots[0].get("coffee"),
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        if usage
        else None,
    }
    # Fire the notifiers (best-effort; each no-ops if unconfigured).
    await notify.notify_review(config, stored)
    await tidbyt.push_review(config, stored)
    return stored
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: PASS — all existing tests plus the new `tests/test_tidbyt.py` (no regressions in `test_smoke.py` / `test_web_auth.py`).

- [ ] **Step 5: Add user docs**

In `deploy/PI_SETUP.md`, add a short "Optional: Tidbyt display" subsection:

```markdown
### Optional: show your latest shot on a Tidbyt

If you have a Tidbyt, crema can push each reviewed shot's score + profile name to it.
It's free (a local render + one API push — no Claude cost) and off unless configured.

1. In the Tidbyt mobile app, get your **device ID** and **API token** (device
   settings → "Get API key", or api.tidbyt.com).
2. Add to your `.env` on the Pi:

   ```bash
   CREMA_TIDBYT_API_TOKEN=your-token
   CREMA_TIDBYT_DEVICE_ID=your-device-id
   ```

3. Restart the service: `sudo systemctl restart crema-web.service`.

The display updates each time a shot is reviewed. Unset either value to turn it off.
```

In `README.md`, add a brief bullet/section under the integrations area (near the Discord mention) titled "Show your latest shot on a Tidbyt" with one or two sentences and a pointer to `deploy/PI_SETUP.md`.

- [ ] **Step 6: Commit**

```bash
git add src/crema/review.py deploy/PI_SETUP.md README.md tests/test_tidbyt.py
git commit -m "feat(tidbyt): fire push on review + document opt-in setup"
```

---

## Self-Review

**Spec coverage:**
- Opt-in / off-by-default → Task 2 config guard + Global Constraints. ✓
- Rides review-completion event → Task 3 wiring next to `notify_review`. ✓
- Score + profile_name data contract → Task 1 render inputs, Task 3 enrichment. ✓
- Pure-Python (no pixlet, armv7) → Task 1 Pillow render. ✓
- Tidbyt push API shape → Task 2. ✓
- Best-effort error handling → Task 2 try/except; Task 1 tolerant render. ✓
- Pillow dependency → Task 1 Step 1. ✓
- Testing (no-op, render bytes, request shape, error swallow) → Tasks 1–2 tests. ✓
- Docs (.env.example, PI_SETUP, README) → Task 2 Step 6, Task 3 Step 5. ✓

**Placeholder scan:** none — every code/step is concrete. README bullet is the one prose-only step (acceptable: it's copy, not logic).

**Type consistency:** `render_frame(score, profile, bean, stale)` and `push_review(config, review)` signatures match across Tasks 1–3; `_score_color` bands match `notify.py`; `stored` keys `profile_name`/`bean` produced in Task 3 are exactly what `push_review` reads in Task 2.

## Manual verification (post-merge, on the Pi)

Not in the automated suite (needs a real device). After deploying with the env vars set, trigger a review (`crema review` or wait for the timer) and confirm the Tidbyt shows the score + profile name within one cycle. With the vars unset, confirm no behavior change.
