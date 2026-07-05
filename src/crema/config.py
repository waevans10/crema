"""crema configuration.

Wraps the vendored GaggimateConfig (device connection + safety limits) and adds
crema-specific settings (Claude models, DB path, review window, web bind).

All settings come from the environment / .env. The Anthropic key is read by the
Anthropic SDK itself from ANTHROPIC_API_KEY — we deliberately do not load it into
this object so it never ends up serialized in a log or a response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

from gaggimate_mcp.config import GaggimateConfig

# Load .env into the process environment so the Anthropic SDK (which reads
# ANTHROPIC_API_KEY from os.environ, not from our settings) picks it up. Search
# from the current working directory — matching pydantic-settings' CWD-relative
# env_file — so `.env` in the project root is found. Real env vars still win.
load_dotenv(find_dotenv(usecwd=True))


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

    # Which machine platform to ingest from: "gaggimate" (default) or
    # "gaggiuino". Reviews/notes/beans/pool work for both; profile drafting
    # and push-back are GaggiMate-only for now. A Literal so a typo in .env
    # (CREMA_MACHINE=Gaggiuino) fails loudly instead of silently defaulting.
    machine: Literal["gaggimate", "gaggiuino"] = "gaggimate"

    # Base URL of the Gaggiuino web server (only used when machine=gaggiuino).
    gaggiuino_url: str = "http://gaggiuino.local"

    # Claude models. Routine per-shot reviews run on the cheaper model; the
    # profile-drafting step escalates to the stronger one.
    review_model: str = "claude-sonnet-5"
    draft_model: str = "claude-opus-4-8"

    # How many recent shots to include as context in a single review.
    review_window: int = 5

    # Your grinder, described in your own words ("Eureka Mignon Specialità,
    # stepless" / "1Zpresso JX-Pro, 30 clicks per rotation"). Lets reviews give
    # grind advice in that grinder's own steps/clicks. The UI/CLI setting
    # (stored in the DB) overrides this env default once set.
    grinder: str = ""

    # The coffee currently in the hopper, in your own words ("Ethiopian natural,
    # light roast, roasted 2 weeks ago"). Grounds review advice in the beans —
    # light vs dark roasts want different grinds and temperatures. The UI/CLI
    # setting (stored in the DB) overrides this env default once set.
    coffee: str = ""

    # Community shot pool endpoint for the opt-in `crema share` command.
    # Empty = sharing disabled. Sharing NEVER happens automatically.
    share_url: str = ""

    # Whether the scheduled timer auto-reviews new shots. This is the *default*;
    # the UI/CLI toggle (stored in the DB) overrides it once set. Off by default so
    # nothing spends automatically until you turn it on.
    autoreview: bool = False

    # Beans older than this many days past their roast date show an "aging"
    # warning in the web UI (past-peak flavour / faster flow). 0 disables it.
    bean_max_age_days: int = 30

    # Retention: shots (and their reviews/edits) older than this many days are
    # pruned on each ingest, so the DB stays bounded. ~3-4 shots/day * 30 days is
    # tiny. Set 0 to keep everything forever.
    retention_days: int = 30

    # Web UI bind. 127.0.0.1 = loopback (view via SSH tunnel); 0.0.0.0 = reachable
    # on the LAN. If you bind to the LAN, set a web password below.
    host: str = "127.0.0.1"
    port: int = 8765

    # Optional HTTP Basic auth for the web UI. Empty password = no auth (fine for
    # loopback). Set a password when binding to the LAN.
    web_user: str = "crema"
    web_password: str = ""

    # Optional Discord webhook. When set, crema posts a message each time a shot is
    # reviewed, with its 1-10 score and the diagnosis. Empty = disabled.
    discord_webhook_url: str = ""

    def gaggimate(self) -> GaggimateConfig:
        """Build the vendored device config (reads its own GAGGIMATE_* env vars)."""
        return GaggimateConfig()
