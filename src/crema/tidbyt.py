"""Tidbyt integration: render the latest reviewed shot and push it to a Tidbyt.

Opt-in and best-effort — a no-op unless CREMA_TIDBYT_API_TOKEN and
CREMA_TIDBYT_DEVICE_ID are set, and any failure is logged and swallowed so a
display problem never breaks a review (same contract as notify.py). Pure-Python
render (Pillow) so it runs on the 32-bit armv7 Pi crema targets — no pixlet.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Optional

import aiohttp
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
