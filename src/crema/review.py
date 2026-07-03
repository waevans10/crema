"""Run a Claude review over the most recent shots and store the result."""

from __future__ import annotations

from typing import Any, Optional

import aiosqlite
from anthropic import AsyncAnthropic

from . import db
from .config import CremaConfig
from .prompts import SYSTEM_PROMPT, ReviewResult, build_user_message


async def review_recent(conn: aiosqlite.Connection, config: CremaConfig) -> Optional[dict[str, Any]]:
    """Review the newest `review_window` shots and persist the suggestions.

    Returns the stored review dict, or None if there are no shots to review.
    The Anthropic client reads ANTHROPIC_API_KEY from the environment.
    """
    shots = await db.recent_shots(conn, limit=config.review_window)
    if not shots:
        return None

    client = AsyncAnthropic()
    response = await client.messages.parse(
        model=config.review_model,
        max_tokens=2048,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": build_user_message(shots)}],
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
    review_id = await db.insert_review(conn, newest_shot_id, config.review_model, suggestions)
    return {
        "id": review_id,
        "shot_id": newest_shot_id,
        "model": config.review_model,
        "suggestions": suggestions,
    }
