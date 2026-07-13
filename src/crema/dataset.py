"""Versioned canonical community-dataset contract and SQLite importer."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recipe(_Strict):
    target_dose_g: Optional[float] = None
    target_yield_g: Optional[float] = None
    profile_target_configured: bool


class Bean(_Strict):
    bean_code: str
    roast_level: Literal["light", "medium-light", "medium", "medium-dark", "dark", "unknown"]
    process: Literal["washed", "natural", "honey", "anaerobic", "other", "unknown"]
    roast_age_days: Optional[int] = Field(default=None, ge=0)
    recipe: Optional[Recipe] = None


class Outcome(_Strict):
    cup_rating: Optional[int] = Field(default=None, ge=1, le=5)
    taste_tags: list[str] = Field(default_factory=list)


class CommunityShot(_Strict):
    shot_index: int = Field(ge=1)
    relative_day: Optional[int] = Field(default=None, ge=0)
    bean: Bean
    outcome: Outcome
    telemetry: dict[str, Any]


class CommunityReview(_Strict):
    shot_index: int = Field(ge=1)
    assistant_used: Literal[True]
    confidence: Optional[Literal["low", "medium", "high"]] = None
    execution_score: Optional[int] = Field(default=None, ge=1, le=10)


class CommunityExperiment(_Strict):
    experiment_index: int = Field(ge=1)
    variable: Literal["grind", "dose", "yield", "temperature", "pressure", "flow", "preinfusion", "puck_prep", "other"]
    direction: Literal["finer", "coarser", "increase", "decrease", "prepare", "other"]
    magnitude: Optional[float] = Field(default=None, ge=0, le=1000)
    unit: Literal["grinder_steps", "g", "c", "bar", "ml_s", "seconds", "none"]
    baseline_execution_score: Optional[int] = Field(default=None, ge=1, le=10)
    baseline_cup_rating: Optional[int] = Field(default=None, ge=1, le=5)
    followup_shot_indices: list[int] = Field(default_factory=list)
    status: Literal["active", "closed"]


class CommunityBundle(_Strict):
    schema_version: Literal[2]
    participant_id: str = Field(min_length=36, max_length=36)
    machine: Literal["gaggimate", "gaggiuino"]
    shots: list[CommunityShot]
    reviews: list[CommunityReview]
    experiments: list[CommunityExperiment]


def validate_bundle(bundle: dict[str, Any]) -> CommunityBundle:
    """Reject unknown, malformed, or incompatible community data."""
    parsed = CommunityBundle.model_validate(bundle)
    shot_indices = {shot.shot_index for shot in parsed.shots}
    if len(shot_indices) != len(parsed.shots):
        raise ValueError("shot_index values must be unique")
    if any(r.shot_index not in shot_indices for r in parsed.reviews):
        raise ValueError("review references a missing shot")
    if any(i not in shot_indices for e in parsed.experiments for i in e.followup_shot_indices):
        raise ValueError("experiment references a missing follow-up shot")
    return parsed


IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS community_bundles (
    participant_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    machine TEXT NOT NULL,
    imported_at REAL NOT NULL DEFAULT (unixepoch('now'))
);
CREATE TABLE IF NOT EXISTS community_shots (
    participant_id TEXT NOT NULL,
    shot_index INTEGER NOT NULL,
    relative_day INTEGER,
    bean_json TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    telemetry_json TEXT NOT NULL,
    PRIMARY KEY (participant_id, shot_index)
);
CREATE TABLE IF NOT EXISTS community_experiments (
    participant_id TEXT NOT NULL,
    experiment_index INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (participant_id, experiment_index)
);
"""


async def import_bundle(conn: aiosqlite.Connection, bundle: dict[str, Any]) -> CommunityBundle:
    """Validate then atomically upsert a share bundle into a pool SQLite DB."""
    parsed = validate_bundle(bundle)
    await conn.executescript(IMPORT_SCHEMA)
    await conn.execute(
        "INSERT INTO community_bundles (participant_id, schema_version, machine) VALUES (?, ?, ?) "
        "ON CONFLICT(participant_id) DO UPDATE SET schema_version=excluded.schema_version, machine=excluded.machine, imported_at=unixepoch('now')",
        (parsed.participant_id, parsed.schema_version, parsed.machine),
    )
    for shot in parsed.shots:
        await conn.execute(
            "INSERT INTO community_shots VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(participant_id, shot_index) DO UPDATE SET relative_day=excluded.relative_day, bean_json=excluded.bean_json, outcome_json=excluded.outcome_json, telemetry_json=excluded.telemetry_json",
            (parsed.participant_id, shot.shot_index, shot.relative_day, json.dumps(shot.bean.model_dump()), json.dumps(shot.outcome.model_dump()), json.dumps(shot.telemetry)),
        )
    for experiment in parsed.experiments:
        await conn.execute(
            "INSERT INTO community_experiments VALUES (?, ?, ?) ON CONFLICT(participant_id, experiment_index) DO UPDATE SET data_json=excluded.data_json",
            (parsed.participant_id, experiment.experiment_index, json.dumps(experiment.model_dump())),
        )
    await conn.commit()
    return parsed
