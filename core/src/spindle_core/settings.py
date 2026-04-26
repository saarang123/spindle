"""Process-wide settings.

Loaded from environment variables (prefix `SPINDLE_`) and optionally from a
`.env` file in the working directory. Every var has a default suitable for
local dev; production deployments override via the environment.

Scope (this iteration): Mongo state backend. Queue, artifact, API, worker,
dispatcher settings will land alongside their respective components.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPINDLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── deployment identity ─────────────────────────────────────────
    node: str = "control"

    # ─── backend selection ───────────────────────────────────────────
    state_backend: Literal["mongo"] = "mongo"

    # ─── mongo ───────────────────────────────────────────────────────
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "spindle"

    # ─── state-store knobs ───────────────────────────────────────────
    # Validate Pydantic models on read from the store. Default True (safe).
    # Set False only if you've profiled and from_doc is a hot path; see
    # spindle_core/state/_serialization.py for the trade-offs.
    state_validate_on_read: bool = True

    # ─── logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
