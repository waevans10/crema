"""Anonymized shot-data export, and opt-in sharing to the community pool.

The bundle is the dataset format: shots in chronological order with their
telemetry, beans, and tasting notes, plus every review's advice — enough to
reconstruct (context → advice → next shot → outcome) training examples.

Identity is a random install UUID generated once and stored in settings.
Nothing else identifying is added — but free-text fields (profile labels,
coffee, tasting notes, grinder) are whatever the barista typed, so `crema
export` exists precisely so you can read what would be shared before sharing.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import aiohttp
import aiosqlite

from . import db
from .config import CremaConfig
from .dataset import validate_bundle

SCHEMA_VERSION = 2

# Bump when SHARE_TERMS changes materially. Stamped into every shared bundle
# (with the acceptance time) so each stored submission carries its own
# evidence of which terms were accepted.
TERMS_VERSION = 2

# What opting in means. Shown by `crema share` and must be accepted explicitly.
SHARE_TERMS = """\
Sharing sends this export bundle to the crema community shot pool:
  * normalized shot telemetry and controlled diagnostic categories
  * recipe targets, controlled taste tags, cup ratings, and structured experiments
  * no coffee names, profile labels, grinder descriptions, free-text notes, or AI prose
  * a random participant id (no name, email, or network details)

By sharing you grant the crema project a perpetual, worldwide, royalty-free
license to use, modify, and redistribute this data, INCLUDING COMMERCIALLY.
The pooled dataset is published for community use under CC BY-NC 4.0
(non-commercial, attribution).

Withdrawal: you can request deletion of your submitted bundles at any time by
opening a GitHub issue with your participant id (shown by `crema export`). Raw
submissions are then removed from the pool; data already included in a
published dataset release stays licensed as released.

Auto-share: if you enable it (`crema autoshare on`), a fresh snapshot is sent
automatically after each review — no further prompts — until you run
`crema autoshare off`. If these terms ever change, auto-share pauses until
you re-accept.

Run `crema export` first if you want to read exactly what leaves your box.\
"""


def stamp_acceptance(bundle: dict[str, Any]) -> dict[str, Any]:
    """Record which terms were accepted, and when, inside the bundle itself."""
    import datetime as _dt

    bundle["terms_version"] = TERMS_VERSION
    bundle["terms_accepted_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return bundle


async def get_install_id(conn: aiosqlite.Connection) -> str:
    """Return this install's random UUID, creating it on first use."""
    existing = await db.get_setting(conn, "install_id")
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    await db.set_setting(conn, "install_id", new_id)
    return new_id


async def build_export_bundle(conn: aiosqlite.Connection, config: CremaConfig) -> dict[str, Any]:
    """Assemble a canonical, privacy-safe dataset bundle from the local DB.

    This intentionally differs from a backup: identifiers, raw dates, free text,
    profile labels, and raw model prose remain local. The exported shape is stable
    enough for aggregate analysis and advice→outcome research.
    """
    install_id = await get_install_id(conn)
    shots = list(reversed(await db.recent_shots(conn, limit=100_000)))
    beans = {b["id"]: b for b in await db.list_beans(conn, limit=100_000)}
    async with conn.execute(
        "SELECT shot_id, model, suggestions, created_at FROM reviews ORDER BY created_at ASC, id ASC"
    ) as cur:
        review_rows = await cur.fetchall()
    async with conn.execute("SELECT * FROM experiments ORDER BY created_at ASC, id ASC") as cur:
        experiment_rows = await cur.fetchall()
    async with conn.execute("SELECT experiment_id, shot_id FROM experiment_shots") as cur:
        experiment_shots = await cur.fetchall()

    shot_index = {s["id"]: i + 1 for i, s in enumerate(shots)}
    first_time = next((s["captured_at"] for s in shots if s.get("captured_at") is not None), None)
    bean_codes = {bid: f"bean_{i + 1}" for i, bid in enumerate(sorted(beans))}

    def relative_day(captured_at: Any) -> int | None:
        try:
            return max(0, int((float(captured_at) - float(first_time)) // 86400))
        except (TypeError, ValueError):
            return None

    def bean_export(bean_id: Any, captured_at: Any) -> dict[str, Any]:
        bean = beans.get(bean_id)
        if not bean:
            return {"bean_code": "unknown", "roast_level": "unknown", "process": "unknown", "roast_age_days": None}
        age: int | None = None
        if bean.get("roast_date") and captured_at is not None:
            try:
                age = max(0, (dt.datetime.fromtimestamp(float(captured_at), tz=dt.timezone.utc).date() - dt.date.fromisoformat(bean["roast_date"])).days)
            except (ValueError, TypeError, OSError, OverflowError):
                pass
        return {
            "bean_code": bean_codes[bean_id], "roast_level": bean["roast_level"],
            "process": bean.get("process") or "unknown", "roast_age_days": age,
            "recipe": {
                "target_dose_g": bean.get("target_dose_g"), "target_yield_g": bean.get("target_yield_g"),
                "profile_target_configured": bool(bean.get("target_profile_id")),
            },
        }

    controlled_tastes = (
        "sour", "sharp", "thin", "salty", "quick finish", "sweet", "balanced", "syrupy", "long finish",
        "bitter", "harsh", "astringent", "drying", "hollow", "weak / watery", "too intense / muddy",
    )
    def taste_tags(notes: Any) -> list[str]:
        text = str(notes or "").lower()
        return [tag for tag in controlled_tastes if tag in text]

    def telemetry(t: dict[str, Any]) -> dict[str, Any]:
        # Whitelisted numeric/controlled diagnostic shape; no names, IDs, phase
        # labels, raw samples, or user-authored profile fields leave the device.
        return {
            "duration_seconds": t.get("duration_seconds"), "final_weight_g": t.get("final_weight_g"),
            "summary": t.get("summary"), "diagnostics": t.get("diagnostics"),
        }

    followups: dict[int, list[int]] = {}
    for row in experiment_shots:
        if row["shot_id"] in shot_index:
            followups.setdefault(row["experiment_id"], []).append(shot_index[row["shot_id"]])

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "participant_id": install_id,  # random UUID, solely for withdrawal/deduplication
        "machine": "gaggimate",
        "shots": [
            {
                "shot_index": shot_index[s["id"]], "relative_day": relative_day(s.get("captured_at")),
                "bean": bean_export(s.get("bean_id"), s.get("captured_at")),
                "outcome": {"cup_rating": s.get("cup_rating"), "taste_tags": taste_tags(s.get("tasting_notes"))},
                "telemetry": telemetry(s["transformed"]),
            }
            for s in shots
        ],
        "reviews": [
            {
                "shot_index": shot_index[r["shot_id"]],
                "assistant_used": True,
                "confidence": json.loads(r["suggestions"]).get("confidence"),
                "execution_score": json.loads(r["suggestions"]).get("score"),
            }
            for r in review_rows if r["shot_id"] in shot_index
        ],
        "experiments": [
            {
                "experiment_index": i + 1,
                "variable": r["variable"] or "other", "direction": r["direction"] or "other",
                "magnitude": r["magnitude"], "unit": r["unit"] or "none",
                "baseline_execution_score": r["baseline_score"], "baseline_cup_rating": r["baseline_cup"],
                "followup_shot_indices": followups.get(r["id"], []), "status": r["status"],
            }
            for i, r in enumerate(experiment_rows)
        ],
    }
    # Keep exporter and future pool importer on the exact same contract.
    return validate_bundle(bundle).model_dump()


# Where `crema share` discovers the current pool endpoint. A pointer in the
# repo — not a baked-in URL — so the Worker can move accounts/domains freely:
# updating this file redirects every install ever shipped, old versions too.
# The repo is already the project's trust root (users run its code), so the
# pointer adds no new trust surface. CREMA_SHARE_URL overrides it entirely.
POOL_URL_POINTER = "https://raw.githubusercontent.com/waevans10/crema/main/.pool-url"


async def resolve_share_url(config: CremaConfig) -> Optional[str]:
    """The pool endpoint: explicit CREMA_SHARE_URL, else the repo pointer.

    Returns None when sharing isn't available (pointer missing/empty — the
    pool isn't live yet). Only https endpoints are accepted.
    """
    if config.share_url.strip().lower() in ("off", "none", "disabled"):
        return None
    if config.share_url:
        return config.share_url
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(POOL_URL_POINTER) as resp:
                if resp.status != 200:
                    return None
                text = (await resp.text()).strip()
                url = text.splitlines()[0].strip() if text else ""
    except aiohttp.ClientError:
        return None
    return url if url.startswith("https://") else None


async def record_autoshare_consent(conn: aiosqlite.Connection) -> None:
    """Store the opt-in: which terms version was accepted, and when.

    Every auto-shared bundle is stamped with THIS stored consent (not a fresh
    timestamp), so each object in the pool carries evidence of the actual
    opt-in moment.
    """
    import datetime as _dt

    await db.set_setting(conn, "autoshare", "1")
    await db.set_setting(conn, "autoshare_terms_version", str(TERMS_VERSION))
    await db.set_setting(
        conn, "autoshare_accepted_at", _dt.datetime.now(_dt.timezone.utc).isoformat()
    )


async def maybe_autoshare(conn: aiosqlite.Connection, config: CremaConfig) -> Optional[str]:
    """Auto-share a snapshot after a review, if (and only if) the barista opted in.

    Best-effort: failures are reported as a message, never raised — a broken
    pool endpoint must not break the review cycle. Returns a human-readable
    status line, or None when auto-share is off / not applicable.
    """
    if not await db.get_bool_setting(conn, "autoshare", False):
        return None
    consent_version = await db.get_setting(conn, "autoshare_terms_version")
    consent_at = await db.get_setting(conn, "autoshare_accepted_at")
    if consent_version != str(TERMS_VERSION) or not consent_at:
        return (
            "auto-share paused: the sharing terms changed since you opted in — "
            "run `crema autoshare on` to review and re-accept"
        )
    try:
        share_url = await resolve_share_url(config)
        if not share_url:
            return None  # pool not live / sharing disabled by override
        bundle = await build_export_bundle(conn, config)
        if not bundle["shots"]:
            return None
        bundle["terms_version"] = TERMS_VERSION
        bundle["terms_accepted_at"] = consent_at
        await share_bundle(bundle, share_url)
        return (
            f"auto-shared {len(bundle['shots'])} shot(s) / {len(bundle['reviews'])} "
            "review(s) to the community pool"
        )
    except Exception as e:  # noqa: BLE001 — never let sharing break the review
        return f"auto-share failed (will retry after the next review): {e}"


async def share_bundle(bundle: dict[str, Any], share_url: str) -> str:
    """POST the bundle to the community pool endpoint. Returns the server's reply."""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            share_url,
            json=bundle,
            headers={"User-Agent": "crema-share/1"},
        ) as resp:
            text = (await resp.text())[:500]
            if resp.status != 200:
                raise RuntimeError(f"Share failed (HTTP {resp.status}): {text}")
            return text
