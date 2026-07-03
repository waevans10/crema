"""Pydantic models for shot ratings and feedback."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class BalanceTaste(str, Enum):
    """Balance/taste profile enumeration."""

    BITTER = "bitter"
    BALANCED = "balanced"
    SOUR = "sour"


class ShotRating(BaseModel):
    """Complete shot rating and tasting notes."""

    shot_id: str = Field(description="Shot ID")
    rating: Optional[int] = Field(
        default=None,
        ge=0,
        le=5,
        description="Star rating (0-5)"
    )
    dose_in: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Coffee dose in grams"
    )
    dose_out: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Espresso output in grams"
    )
    ratio: Optional[float] = Field(
        default=None,
        description="Extraction ratio (calculated from dose_in/dose_out)"
    )
    grind_setting: Optional[str] = Field(
        default=None,
        description="Grinder setting used"
    )
    balance_taste: Optional[BalanceTaste] = Field(
        default=None,
        description="Taste balance profile"
    )
    bean_type: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Coffee bean type/origin"
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Tasting notes and observations"
    )

    @model_validator(mode="after")
    def calculate_ratio(self):
        """Calculate extraction ratio from dose_in and dose_out."""
        if self.dose_in and self.dose_out:
            self.ratio = round(self.dose_out / self.dose_in, 3)
        return self


class RatingUpdate(BaseModel):
    """Rating update for updating existing shot ratings."""

    shot_id: str = Field(description="Shot ID to update")
    rating: Optional[int] = Field(
        default=None,
        ge=0,
        le=5,
        description="Star rating (0-5)"
    )
    dose_in: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Coffee dose in grams"
    )
    dose_out: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Espresso output in grams"
    )
    grind_setting: Optional[str] = Field(
        default=None,
        description="Grinder setting used"
    )
    balance_taste: Optional[BalanceTaste] = Field(
        default=None,
        description="Taste balance profile"
    )
    bean_type: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Coffee bean type/origin"
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Tasting notes and observations"
    )
