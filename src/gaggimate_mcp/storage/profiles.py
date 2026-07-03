"""Profile version storage.

Stores profile versions created by the AI agent locally for tracking
and comparison purposes. Profiles are saved in JSON format with versioning.
"""

import json
from pathlib import Path
from typing import Optional
from datetime import datetime

from gaggimate_mcp.config import GaggimateConfig
from gaggimate_mcp.logging_config import get_logger

logger = get_logger(__name__)


class ProfileStorage:
    """Local storage for AI-generated profile versions."""

    def __init__(self, config: Optional[GaggimateConfig] = None):
        """Initialize profile storage.

        Args:
            config: Configuration object (uses default if None)
        """
        self.config = config or GaggimateConfig()
        self.storage_dir = Path(self.config.storage_path) / "profiles"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("profile_storage_initialized", path=str(self.storage_dir))

    def _get_profile_path(self, profile_name: str, version: int) -> Path:
        """Get path for a profile version file.

        Args:
            profile_name: Profile name (will be sanitized)
            version: Version number

        Returns:
            Path to profile file
        """
        # Sanitize profile name for filesystem
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_name)
        filename = f"Agent-{safe_name}_v{version}.json"
        return self.storage_dir / filename

    def _get_next_version(self, profile_name: str) -> int:
        """Get next version number for a profile.

        Args:
            profile_name: Profile name

        Returns:
            Next version number (1 if no versions exist)
        """
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_name)
        pattern = f"Agent-{safe_name}_v*.json"

        existing_files = list(self.storage_dir.glob(pattern))
        if not existing_files:
            return 1

        versions = []
        for file_path in existing_files:
            try:
                # Extract version from filename like "Agent-Name_v3.json"
                version_part = file_path.stem.split("_v")[1]
                versions.append(int(version_part))
            except (IndexError, ValueError):
                continue

        return max(versions) + 1 if versions else 1

    def save_profile_version(
        self,
        profile_name: str,
        profile_data: dict,
        metadata: Optional[dict] = None
    ) -> dict:
        """Save a new profile version.

        Args:
            profile_name: Profile name
            profile_data: Complete profile dictionary
            metadata: Optional metadata (notes, reasoning, etc.)

        Returns:
            Dictionary with save information
        """
        version = self._get_next_version(profile_name)
        file_path = self._get_profile_path(profile_name, version)

        # Build storage object
        storage_data = {
            "version": version,
            "profile_name": profile_name,
            "timestamp": datetime.now().isoformat(),
            "profile": profile_data,
            "metadata": metadata or {},
        }

        # Save to disk
        with open(file_path, "w") as f:
            json.dump(storage_data, f, indent=2)

        logger.info(
            "profile_version_saved",
            profile_name=profile_name,
            version=version,
            path=str(file_path)
        )

        return {
            "profile_name": profile_name,
            "version": version,
            "file_path": str(file_path),
            "timestamp": storage_data["timestamp"],
        }

    def list_profile_versions(self, profile_name: Optional[str] = None) -> list[dict]:
        """List saved profile versions.

        Args:
            profile_name: Optional profile name filter

        Returns:
            List of profile version summaries
        """
        if profile_name:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_name)
            pattern = f"Agent-{safe_name}_v*.json"
        else:
            pattern = "Agent-*_v*.json"

        versions = []
        for file_path in sorted(self.storage_dir.glob(pattern)):
            try:
                with open(file_path) as f:
                    data = json.load(f)

                versions.append({
                    "profile_name": data.get("profile_name"),
                    "version": data.get("version"),
                    "timestamp": data.get("timestamp"),
                    "file_path": str(file_path),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("failed_to_read_profile_version", path=str(file_path), error=str(e))
                continue

        logger.debug("profile_versions_listed", count=len(versions), filter=profile_name)
        return versions

    def load_profile_version(self, profile_name: str, version: int) -> Optional[dict]:
        """Load a specific profile version.

        Args:
            profile_name: Profile name
            version: Version number

        Returns:
            Profile data dictionary, or None if not found
        """
        file_path = self._get_profile_path(profile_name, version)

        if not file_path.exists():
            logger.warning("profile_version_not_found", profile_name=profile_name, version=version)
            return None

        try:
            with open(file_path) as f:
                data = json.load(f)

            logger.info("profile_version_loaded", profile_name=profile_name, version=version)
            return data

        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "failed_to_load_profile_version",
                profile_name=profile_name,
                version=version,
                error=str(e)
            )
            return None

    def get_latest_version(self, profile_name: str) -> Optional[dict]:
        """Get the most recent version of a profile.

        Args:
            profile_name: Profile name

        Returns:
            Latest profile data, or None if no versions exist
        """
        versions = self.list_profile_versions(profile_name)
        if not versions:
            return None

        # Sort by version number descending
        latest = max(versions, key=lambda v: v["version"])
        return self.load_profile_version(profile_name, latest["version"])
