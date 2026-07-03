"""crema configuration.

Wraps the vendored GaggimateConfig (device connection + safety limits) and adds
crema-specific settings (Claude models, DB path, review window, web bind).

All settings come from the environment / .env. The Anthropic key is read by the
Anthropic SDK itself from ANTHROPIC_API_KEY — we deliberately do not load it into
this object so it never ends up serialized in a log or a response.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from gaggimate_mcp.config import GaggimateConfig


class CremaConfig(BaseSettings):
    """crema application settings (env prefix: CREMA_)."""

    model_config = SettingsConfigDict(
        env_prefix="CREMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    db_path: Path = Path("./crema.db")

    # Claude models. Routine per-shot reviews run on the cheaper model; the
    # profile-drafting step escalates to the stronger one.
    review_model: str = "claude-sonnet-5"
    draft_model: str = "claude-opus-4-8"

    # How many recent shots to include as context in a single review.
    review_window: int = 5

    # Web UI bind. Default to loopback — expose remotely with a tunnel, never by
    # binding 0.0.0.0 without auth in front of it.
    host: str = "127.0.0.1"
    port: int = 8765

    def gaggimate(self) -> GaggimateConfig:
        """Build the vendored device config (reads its own GAGGIMATE_* env vars)."""
        return GaggimateConfig()
