"""Discord notifications: post a shot's score + diagnosis when it's reviewed.

Best-effort — any failure is swallowed so a webhook problem never breaks a review.
Enabled only when CREMA_DISCORD_WEBHOOK_URL is set.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .config import CremaConfig


def _score_color(score: int) -> int:
    if score < 4:
        return 0xC0392B  # red
    if score < 7:
        return 0xC2871A  # amber
    return 0x3F8F43  # green


async def notify_review(config: CremaConfig, review: dict[str, Any]) -> None:
    """Post a Discord embed for a reviewed shot. No-op if no webhook is configured."""
    url = config.discord_webhook_url
    if not url:
        return

    s = review.get("suggestions", {})
    try:
        score = int(s.get("score") or 5)
    except (TypeError, ValueError):
        score = 5
    score = max(1, min(10, score))

    def field(value: str) -> str:
        # Discord rejects empty field values.
        return (value or "—")[:250]

    payload = {
        "embeds": [
            {
                "title": f"☕ Shot {review.get('shot_id', '?')} — {score}/10",
                "description": field(s.get("diagnosis", "")),
                "color": _score_color(score),
                "fields": [
                    {"name": "Grind", "value": field(s.get("grind_change", "")), "inline": True},
                    {"name": "Dose / yield", "value": field(s.get("dose_yield_change", "")), "inline": True},
                ],
                "footer": {"text": f"crema · {review.get('model', '')}"},
            }
        ]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(url, json=payload)
    except Exception:  # noqa: BLE001 — notifications must never break a review
        pass
