"""Rating storage for shot feedback.

Stores shot ratings and tasting notes locally since the device
doesn't support these features directly.
"""

import json
from pathlib import Path
from typing import Optional
from datetime import datetime

from gaggimate_mcp.config import GaggimateConfig
from gaggimate_mcp.logging_config import get_logger
from gaggimate_mcp.models.rating import ShotRating

logger = get_logger(__name__)


class RatingStorage:
    """Local storage for shot ratings and notes."""

    def __init__(self, config: Optional[GaggimateConfig] = None):
        """Initialize rating storage.

        Args:
            config: Configuration object (uses default if None)
        """
        self.config = config or GaggimateConfig()
        self.storage_path = Path(self.config.storage_path) / "ratings.json"
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing ratings
        self.ratings: dict[str, dict] = self._load()
        logger.debug("rating_storage_initialized", path=str(self.storage_path))

    def _load(self) -> dict[str, dict]:
        """Load ratings from disk.

        Returns:
            Dictionary mapping shot_id to rating data
        """
        if not self.storage_path.exists():
            return {}

        try:
            with open(self.storage_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("failed_to_load_ratings", error=str(e))
            return {}

    def _save(self) -> None:
        """Save ratings to disk."""
        try:
            with open(self.storage_path, "w") as f:
                json.dump(self.ratings, f, indent=2)
        except OSError as e:
            logger.error("failed_to_save_ratings", error=str(e))

    def save_rating(self, rating: ShotRating) -> dict:
        """Save or update a shot rating.

        Args:
            rating: Shot rating object

        Returns:
            Confirmation dictionary
        """
        # Store as dictionary
        rating_data = {
            "shot_id": rating.shot_id,
            "rating": rating.rating,
            "notes": rating.notes,
            "timestamp": datetime.now().isoformat(),
        }

        self.ratings[rating.shot_id] = rating_data
        self._save()

        logger.info("rating_saved", shot_id=rating.shot_id, rating=rating.rating)
        return rating_data

    def get_rating(self, shot_id: str) -> Optional[dict]:
        """Get rating for a specific shot.

        Args:
            shot_id: Shot identifier

        Returns:
            Rating dictionary, or None if not found
        """
        return self.ratings.get(shot_id)

    def list_ratings(self) -> list[dict]:
        """List all saved ratings.

        Returns:
            List of rating dictionaries
        """
        return list(self.ratings.values())

    def delete_rating(self, shot_id: str) -> bool:
        """Delete a rating.

        Args:
            shot_id: Shot identifier

        Returns:
            True if deleted, False if not found
        """
        if shot_id in self.ratings:
            del self.ratings[shot_id]
            self._save()
            logger.info("rating_deleted", shot_id=shot_id)
            return True

        logger.warning("rating_not_found", shot_id=shot_id)
        return False
