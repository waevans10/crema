"""Pydantic models for espresso brewing profiles."""

from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


class PumpSettings(BaseModel):
    """Pump control settings for a phase."""

    target: Literal["pressure", "flow"] = Field(
        description="Control target: pressure or flow"
    )
    pressure: float = Field(
        ge=0.0,
        le=12.0,
        description="Pressure in bar (0-12)"
    )
    flow: float = Field(
        ge=0.0,
        description="Flow rate in ml/s"
    )


class TransitionSettings(BaseModel):
    """Transition settings between phases."""

    type: Literal["linear", "ease-out", "ease-in", "instant"] = Field(
        description="Transition type"
    )
    duration: float = Field(
        ge=0.0,
        description="Transition duration in seconds"
    )


class TargetCondition(BaseModel):
    """Stop condition for a phase."""

    type: Literal["pressure", "flow", "volumetric", "pumped"] = Field(
        description="Type of stop condition"
    )
    operator: Literal["gte", "lte"] = Field(
        description="Comparison operator (gte = >=, lte = <=)"
    )
    value: float = Field(
        description="Threshold value"
    )


class PhaseData(BaseModel):
    """Single phase in a brewing profile."""

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Phase name (e.g., Preinfusion, Extraction)"
    )
    phase: Literal["preinfusion", "brew"] = Field(
        description="Phase type"
    )
    duration: float = Field(
        gt=0.0,
        le=120.0,
        description="Duration in seconds"
    )
    temperature: Optional[float] = Field(
        default=None,
        ge=60.0,
        le=96.0,
        description="Phase-specific temperature in Celsius (optional)"
    )
    pump: PumpSettings = Field(
        description="Pump control settings"
    )
    transition: Optional[TransitionSettings] = Field(
        default=None,
        description="Transition settings (optional)"
    )
    targets: list[TargetCondition] = Field(
        default_factory=list,
        description="Stop conditions (phase stops when ANY condition is met)"
    )


class ProfileData(BaseModel):
    """Profile data for create/update operations."""

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Profile name (Agent- prefix added automatically)"
    )
    description: str = Field(
        max_length=500,
        description="Profile description"
    )
    temperature: float = Field(
        ge=60.0,
        le=96.0,
        description="Target water temperature in Celsius (60-96°C)"
    )
    phases: list[PhaseData] = Field(
        min_length=1,
        max_length=10,
        description="Brewing phases (1-10 phases)"
    )


class Profile(BaseModel):
    """Complete profile model with metadata."""

    id: Optional[str] = Field(
        default=None,
        description="Profile UUID (empty for new profiles)"
    )
    label: str = Field(
        min_length=1,
        max_length=100,
        description="Profile display name"
    )
    type: Literal["simple", "pro"] = Field(
        default="pro",
        description="Profile type"
    )
    description: str = Field(
        max_length=500,
        description="Profile description"
    )
    temperature: float = Field(
        ge=60.0,
        le=96.0,
        description="Target water temperature in Celsius"
    )
    favorite: bool = Field(
        default=False,
        description="Marked as favorite"
    )
    selected: bool = Field(
        default=False,
        description="Currently selected profile"
    )
    utility: bool = Field(
        default=False,
        description="Utility profile (system profile)"
    )
    phases: list[PhaseData] = Field(
        min_length=1,
        max_length=10,
        description="Brewing phases"
    )
    agent_created: bool = Field(
        default=False,
        description="Whether this profile was created by the agent"
    )

    @model_validator(mode="after")
    def detect_agent_created(self):
        """Auto-detect if profile was created by agent based on label suffix."""
        # Auto-detect from label suffix (uses default, config loaded at runtime)
        from gaggimate_mcp.config import GaggimateConfig
        config = GaggimateConfig()
        self.agent_created = self.label.endswith(config.ai_profile_suffix)
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "profile-uuid-123",
                "label": "Brazilian Medium [AI]",
                "type": "pro",
                "description": "Optimized for Brazilian medium roast beans",
                "temperature": 93.0,
                "favorite": False,
                "selected": False,
                "utility": False,
                "phases": [
                    {
                        "name": "Preinfusion",
                        "phase": "preinfusion",
                        "duration": 8.0,
                        "pump": {
                            "target": "pressure",
                            "pressure": 2.0,
                            "flow": 0.0
                        },
                        "transition": {
                            "type": "linear",
                            "duration": 2.0
                        }
                    },
                    {
                        "name": "Extraction",
                        "phase": "brew",
                        "duration": 30.0,
                        "pump": {
                            "target": "pressure",
                            "pressure": 9.0,
                            "flow": 0.0
                        },
                        "targets": [
                            {
                                "type": "volumetric",
                                "operator": "gte",
                                "value": 36.0
                            }
                        ]
                    }
                ]
            }
        }
    }
