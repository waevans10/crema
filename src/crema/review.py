"""Run a Claude review over the most recent shots and store the result."""

from __future__ import annotations

from typing import Any, Optional

import aiosqlite
from anthropic import AsyncAnthropic

from . import db, notify, tidbyt
from .config import CremaConfig
from .prompts import SYSTEM_PROMPT, ReviewResult, build_user_message


async def review_recent(conn: aiosqlite.Connection, config: CremaConfig) -> Optional[dict[str, Any]]:
    """Review the newest `review_window` shots and persist the suggestions."""
    shots = await db.recent_shots(conn, limit=config.review_window)
    return await _review(conn, config, shots)


async def review_shots(
    conn: aiosqlite.Connection, config: CremaConfig, shot_ids: list[str]
) -> Optional[dict[str, Any]]:
    """Review a specific set of shots (by id) and persist the suggestions."""
    shots: list[dict[str, Any]] = []
    for sid in shot_ids:
        shot = await db.get_shot(conn, sid)
        if shot:
            shots.append(shot)
    return await _review(conn, config, shots)


async def _review(
    conn: aiosqlite.Connection, config: CremaConfig, shots: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Run a Claude review over the given shots (newest first) and store it.

    Returns the stored review dict, or None if there are no shots to review.
    The Anthropic client reads ANTHROPIC_API_KEY from the environment.
    """
    if not shots:
        return None

    # The barista's grinder description (set via UI/CLI, env default) lets Claude
    # phrase grind advice in that grinder's own steps/clicks/numbers; the coffee
    # description grounds advice in the beans (roast level/date).
    grinder = (await db.get_setting(conn, "grinder")) or config.grinder or None
    coffee = (await db.get_setting(conn, "coffee")) or config.coffee or None
    # Past reviews for the older shots in the window, so Claude sees the advice
    # given after each shot and whether the next shot improved.
    prior_reviews = await db.latest_reviews_for_shots(conn, [s["id"] for s in shots[1:]])

    client = AsyncAnthropic()
    response = await client.messages.parse(
        model=config.review_model,
        # Generous budget: on models with adaptive thinking on by default
        # (e.g. Sonnet 5) thinking tokens count against max_tokens, so a small
        # cap stops the model mid-thought before the JSON is emitted.
        max_tokens=8192,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": build_user_message(
                shots, grinder=grinder, coffee=coffee, prior_reviews=prior_reviews
            ),
        }],
        output_format=ReviewResult,
    )

    result = response.parsed_output
    if result is None:
        # Safety refusal or unparseable output — surface it rather than storing junk.
        raise RuntimeError(
            f"Claude did not return a structured review (stop_reason={response.stop_reason})."
        )

    newest_shot_id = shots[0]["id"]
    suggestions = result.model_dump()
    usage = getattr(response, "usage", None)
    review_id = await db.insert_review(
        conn,
        newest_shot_id,
        config.review_model,
        suggestions,
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
    )
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
