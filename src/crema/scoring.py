"""Deterministic, telemetry-only espresso execution scoring.

The score intentionally measures how cleanly the machine executed a shot; it
does not claim to measure whether the coffee tasted good.  Taste belongs to
the barista's separate cup rating.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def execution_score(transformed: dict[str, Any]) -> dict[str, Any]:
    """Return a reproducible 1–10 execution score and its evidence.

    Penalties are deliberately capped. A single noisy sensor cannot turn an
    otherwise well-executed shot into a failing score, while high-confidence
    channeling remains the strongest execution fault.
    """
    diagnostics = transformed.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return {
            "score": 5.0,
            "confidence": "low",
            "reason": "Telemetry is too sparse for an execution assessment.",
            "components": {"data_quality": -5.0},
        }

    penalties: dict[str, float] = {}
    channeling = diagnostics.get("channeling")
    risk = channeling.get("channeling_risk") if isinstance(channeling, dict) else diagnostics.get("channeling_risk")
    channel_penalty = {"LOW": 0.0, "MODERATE": 1.0, "HIGH": 2.2, "VERY_HIGH": 3.4}.get(str(risk), 0.0)
    if channel_penalty:
        penalties["channeling"] = channel_penalty

    temperature = diagnostics.get("temperature")
    stability = _num(temperature.get("stability_std_c")) if isinstance(temperature, dict) else _num(diagnostics.get("temperature_stability_c"))
    if stability is not None and stability > 0.5:
        penalties["temperature_stability"] = min(1.2, round((stability - 0.5) * 0.6, 2))

    compliance = diagnostics.get("profile_compliance")
    if not isinstance(compliance, dict):
        compliance = diagnostics
    pressure_rmse = _num(compliance.get("pressure_rmse_bar"))
    flow_rmse = _num(compliance.get("flow_rmse_ml_s"))
    if pressure_rmse is not None and pressure_rmse > 0.5:
        penalties["pressure_adherence"] = min(1.0, round((pressure_rmse - 0.5) * 0.5, 2))
    if flow_rmse is not None and flow_rmse > 0.35:
        penalties["flow_adherence"] = min(1.4, round((flow_rmse - 0.35) * 0.8, 2))

    resistance = diagnostics.get("resistance")
    erosion = resistance.get("annotations", {}).get("erosion") if isinstance(resistance, dict) else diagnostics.get("annotations", {}).get("resistance_erosion")
    if erosion in {"HIGH", "VERY_HIGH"}:
        penalties["resistance_erosion"] = 0.7 if erosion == "HIGH" else 1.2

    confidence = "high"
    if risk == "INSUFFICIENT_DATA":
        confidence = "low"
    elif flow_rmse is None or not isinstance(compliance, dict) or compliance is diagnostics:
        confidence = "medium"

    score = max(1.0, round(10.0 - sum(penalties.values()), 1))
    if penalties:
        dominant = max(penalties, key=penalties.get).replace("_", " ")
        reason = f"Execution capped by {dominant} ({penalties[dominant.replace(' ', '_')]:g} point penalty)."
    else:
        reason = "Clean execution: no material telemetry faults detected."
    return {"score": score, "confidence": confidence, "reason": reason, "components": {k: -v for k, v in penalties.items()}}
