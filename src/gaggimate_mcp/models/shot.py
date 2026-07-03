"""Pydantic models for espresso shot data."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ShotMetadata(BaseModel):
    """Metadata for an espresso shot."""

    shot_id: str = Field(description="Shot ID")
    profile_name: str = Field(description="Profile name used")
    profile_id: str = Field(description="Profile UUID")
    timestamp: datetime = Field(description="Shot timestamp")
    duration_seconds: float = Field(ge=0.0, description="Shot duration in seconds")
    final_weight_grams: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Final weight in grams (if scale connected)"
    )
    sample_count: int = Field(ge=0, description="Number of samples recorded")
    sample_interval_ms: int = Field(ge=0, description="Sample interval in milliseconds")
    bluetooth_scale_connected: bool = Field(
        default=False,
        description="Whether Bluetooth scale was connected"
    )
    volumetric_mode: bool = Field(
        default=False,
        description="Whether shot was pulled in volumetric mode"
    )


class TemperatureStats(BaseModel):
    """Temperature statistics for a shot."""

    min_celsius: float = Field(description="Minimum temperature")
    max_celsius: float = Field(description="Maximum temperature")
    average_celsius: float = Field(description="Average temperature")
    target_average: float = Field(description="Target average temperature")


class PressureStats(BaseModel):
    """Pressure statistics for a shot."""

    min_bar: float = Field(ge=0.0, description="Minimum pressure")
    max_bar: float = Field(ge=0.0, description="Maximum pressure")
    average_bar: float = Field(ge=0.0, description="Average pressure")
    peak_time_seconds: float = Field(ge=0.0, description="Time of peak pressure")


class FlowStats(BaseModel):
    """Flow statistics for a shot."""

    total_volume_ml: float = Field(ge=0.0, description="Total volume extracted")
    average_flow_rate_ml_s: float = Field(ge=0.0, description="Average flow rate")
    peak_flow_ml_s: float = Field(ge=0.0, description="Peak flow rate")
    time_to_first_drip_seconds: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Time to first drip"
    )


class ExtractionStats(BaseModel):
    """Extraction timing statistics."""

    extraction_time_seconds: float = Field(ge=0.0, description="Total extraction time")
    preinfusion_time_seconds: float = Field(ge=0.0, description="Preinfusion duration")
    main_extraction_seconds: float = Field(ge=0.0, description="Main extraction duration")


class ShotSummary(BaseModel):
    """Summary statistics for a shot."""

    temperature: TemperatureStats = Field(description="Temperature statistics")
    pressure: PressureStats = Field(description="Pressure statistics")
    flow: FlowStats = Field(description="Flow statistics")
    extraction: ExtractionStats = Field(description="Extraction timing")


class ShotPhaseData(BaseModel):
    """Phase data for a shot."""

    name: str = Field(description="Phase name")
    phase_number: int = Field(ge=0, description="Phase number")
    start_time_seconds: float = Field(ge=0.0, description="Phase start time")
    duration_seconds: float = Field(ge=0.0, description="Phase duration")
    sample_count: int = Field(ge=0, description="Number of samples in phase")
    avg_temperature_c: float = Field(description="Average temperature")
    avg_pressure_bar: float = Field(ge=0.0, description="Average pressure")
    total_flow_ml: float = Field(ge=0.0, description="Total flow in phase")


class TransformedShot(BaseModel):
    """Complete shot data transformed for AI analysis."""

    metadata: ShotMetadata = Field(description="Shot metadata")
    summary: ShotSummary = Field(description="Summary statistics")
    phases: list[ShotPhaseData] = Field(
        default_factory=list,
        description="Phase-by-phase breakdown"
    )


class ShotListItem(BaseModel):
    """Shot list item for shot history."""

    id: str = Field(description="Shot ID")
    timestamp: datetime = Field(description="Shot timestamp")
    profile_id: str = Field(description="Profile UUID")
    profile_name: str = Field(description="Profile name")
    duration_seconds: float = Field(ge=0.0, description="Shot duration")
    final_weight_grams: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Final weight (if available)"
    )
    rating: Optional[int] = Field(
        default=None,
        ge=0,
        le=5,
        description="User rating (0-5 stars)"
    )
    has_notes: bool = Field(
        default=False,
        description="Whether shot has tasting notes"
    )
