"""Generate a first-shot STARTING POINT for a brand-new bean.

crema's other flows all react to a shot that already exists. This one is a cold
start: given a structured bean (roast level, origin, process) and the grinder,
find the barista's most SIMILAR past shots, and ask the stronger model for a
starting grind + dose + yield and a complete, push-ready profile — anchored on
what already worked for similar beans rather than on theory.

Similarity is intentionally lightweight (roast-level bucket + name/process token
overlap, parsed from the freetext `coffee` string every shot carries) so it runs
on a Pi with no embeddings and no new dependencies. The result is staged as a
normal pending edit, so it flows through the same human-approval/push path as any
other profile change — nothing reaches the machine without a tap.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import aiosqlite
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from gaggimate_mcp.models.profile import ProfileData

from . import db
from .config import CremaConfig
from .draft import TEMP_MAX, TEMP_MIN, _clamp, _to_device_phase
from .prompts import STARTING_SYSTEM_PROMPT, StartingPoint, build_starting_message

# Roast levels ordered light → dark; index gives adjacency for fuzzy matching.
_ROAST_ORDER = {level: i for i, level in enumerate(db.ROAST_LEVELS)}

# Words that carry no bean identity — dropped before token-overlap scoring so a
# match is driven by origin/varietal/process, not filler.
_STOPWORDS = {
    "roast", "roasted", "coffee", "bean", "beans", "single", "origin", "so",
    "the", "and", "a", "of", "with", "blend", "espresso", "medium", "light",
    "dark", "process", "ago", "week", "weeks", "day", "days", "fresh",
}


def parse_roast_bucket(text: Optional[str]) -> Optional[int]:
    """Map a freetext coffee string to a roast index (0 light … 4 dark), or None.

    Checks the compound levels ('medium-dark', 'medium-light') before the bare
    ones so 'medium-dark' isn't read as plain 'medium' or 'dark'.
    """
    if not text:
        return None
    t = text.lower()
    if re.search(r"medium[\s-]*dark", t):
        return _ROAST_ORDER["medium-dark"]
    if re.search(r"medium[\s-]*light", t):
        return _ROAST_ORDER["medium-light"]
    if "dark" in t:
        return _ROAST_ORDER["dark"]
    if "light" in t or "blonde" in t:
        return _ROAST_ORDER["light"]
    if "medium" in t:
        return _ROAST_ORDER["medium"]
    return None


def _tokens(text: Optional[str]) -> set[str]:
    """Identity-bearing words (origin/varietal/process), lowercased, minus filler."""
    if not text:
        return set()
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def similarity(bean: dict[str, Any], coffee_text: Optional[str]) -> int:
    """Score how similar a past shot's beans (freetext) are to `bean`.

    Roast level dominates (it drives grind and temperature); origin/process token
    overlap refines it. Higher is closer; 0 means unrelated.
    """
    if not coffee_text:
        return 0
    score = 0
    bean_bucket = _ROAST_ORDER.get(bean.get("roast_level", ""))
    other_bucket = parse_roast_bucket(coffee_text)
    if bean_bucket is not None and other_bucket is not None:
        gap = abs(bean_bucket - other_bucket)
        if gap == 0:
            score += 3
        elif gap == 1:
            score += 1
    bean_tokens = _tokens(bean.get("name")) | _tokens(bean.get("process"))
    score += 2 * len(bean_tokens & _tokens(coffee_text))
    if bean.get("process") and bean["process"].lower() in (coffee_text or "").lower():
        score += 1
    return score


def _similarity_label(score: int) -> str:
    return "high" if score >= 5 else "medium" if score >= 3 else "some"


async def find_similar_shots(
    conn: aiosqlite.Connection,
    bean: dict[str, Any],
    limit: int = 3,
    pool: int = 40,
) -> list[dict[str, Any]]:
    """Return the `limit` past shots whose beans are most similar to `bean`.

    Each result carries the shot, its latest review (if any), the cached profile
    it ran (if any), and the similarity score/label. Shots with no meaningful
    similarity (score < 2 — not even an adjacent roast) are excluded, so a totally
    unrelated history doesn't drag the starting point off course.
    """
    shots = await db.recent_shots(conn, limit=pool)
    scored = [(similarity(bean, s.get("coffee")), s) for s in shots]
    scored = [(sc, s) for sc, s in scored if sc >= 2]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:limit]
    reviews = await db.latest_reviews_for_shots(conn, [s["id"] for _, s in top])
    out: list[dict[str, Any]] = []
    for sc, shot in top:
        pid = shot.get("transformed", {}).get("profile_id")
        profile = await db.get_profile(conn, str(pid)) if pid else None
        out.append(
            {
                "shot": shot,
                "review": reviews.get(shot["id"]),
                "profile": profile,
                "similarity": sc,
                "similarity_label": _similarity_label(sc),
            }
        )
    return out


async def generate_starting_point(
    conn: aiosqlite.Connection,
    config: CremaConfig,
    bean: dict[str, Any],
    grinder: Optional[str] = None,
    dose_target: Optional[float] = None,
) -> dict[str, Any]:
    """Generate a starting point for `bean` and stage it as a pending edit.

    Returns {"edit": <pending edit dict>, "starting": <StartingPoint>,
    "similar": [...]}. The profile is validated and bound-clamped exactly like a
    review-drafted edit, and stored with status 'draft' — approval/push is the
    same guarded path as everywhere else. Raises RuntimeError on an invalid draft.
    """
    grinder = grinder or (await db.get_setting(conn, "grinder")) or config.grinder or None
    similar = await find_similar_shots(conn, bean)

    client = AsyncAnthropic()
    response = await client.messages.parse(
        model=config.draft_model,
        max_tokens=8192,
        system=STARTING_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": build_starting_message(
                    bean, grinder=grinder, dose_target=dose_target, similar=similar
                ),
            }
        ],
        output_format=StartingPoint,
    )
    sp = response.parsed_output
    if sp is None:
        raise RuntimeError(
            f"Claude did not return a structured starting point (stop_reason={response.stop_reason})."
        )

    profile = sp.profile
    temperature = _clamp(profile.temperature, TEMP_MIN, TEMP_MAX)
    phases = [_to_device_phase(ph) for ph in profile.phases]
    label = (profile.label or bean["name"])[:100]

    try:
        ProfileData(
            name=label,
            description=(profile.change_summary or sp.rationale)[:500],
            temperature=temperature,
            phases=phases,
        )
    except ValidationError as e:
        raise RuntimeError(f"Starting profile failed validation and was not stored:\n{e}") from e

    profile_json = {
        "label": label,
        "type": "pro",
        "temperature": temperature,
        "phases": phases,
    }
    change_summary = _format_summary(sp, bean, phases, similar)
    edit_id = await db.insert_pending_edit(
        conn,
        label=label,
        change_summary=change_summary,
        profile=profile_json,
        review_id=None,
        base_profile_id=None,
        base_profile_label=f"new bean · {bean['name']}",
        notes=None,
        # Brand-new profile: there is no existing profile whose stop conditions we
        # could be changing silently, so nothing to acknowledge. The stops are
        # surfaced in the summary instead, for transparency.
        stop_changes=None,
    )
    edit = await db.get_pending_edit(conn, edit_id)
    assert edit is not None
    return {"edit": edit, "starting": sp, "similar": similar}


_OP_TXT = {"gte": "≥", "lte": "≤"}


def _format_summary(
    sp: StartingPoint,
    bean: dict[str, Any],
    phases: list[dict[str, Any]],
    similar: list[dict[str, Any]],
) -> str:
    """Human summary shown on the staged edit: grind/dose/yield, rationale, stops."""
    lines = [
        f"Starting point for {bean['name']} ({bean['roast_level']} roast"
        + (f", {bean['process']}" if bean.get("process") else "")
        + ").",
        f"Grind: {sp.grind_setting}",
        f"Dose: {sp.dose_g:g}g → yield {sp.yield_g:g}g ({sp.ratio})",
    ]
    # Surface the profile's own stop conditions so the barista sees when it ends.
    stops = []
    for ph in phases:
        for t in ph.get("targets", []):
            stops.append(
                f"{ph.get('name', 'phase')} stops at {t.get('type')} "
                f"{_OP_TXT.get(str(t.get('operator')), t.get('operator'))} {t.get('value')}"
            )
    if stops:
        lines.append("Stops: " + "; ".join(stops))
    if similar:
        anchored = ", ".join(
            f"{s['shot']['id']} ({s['similarity_label']})" for s in similar
        )
        lines.append(f"Anchored on similar past shots: {anchored}.")
    else:
        lines.append("No similar past shots — built from roast-level first principles.")
    lines.append(sp.rationale)
    return "\n".join(lines)
