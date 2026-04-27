"""Process-wide settings.

Loaded from environment variables (prefix `SPINDLE_`) and optionally from a
`.env` file in the working directory. Every var has a default suitable for
local dev; production deployments override via the environment.

Scope (this iteration): Mongo state backend. Queue, artifact, API, worker,
dispatcher settings will land alongside their respective components.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
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
    queue_backend: Literal["redis", "memory"] = "redis"
    artifact_backend: Literal["s3", "memory"] = "s3"

    # ─── mongo ───────────────────────────────────────────────────────
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "spindle"

    # ─── state-store knobs ───────────────────────────────────────────
    # Validate Pydantic models on read from the store. Default True (safe).
    # Set False only if you've profiled and from_doc is a hot path; see
    # spindle_core/state/_serialization.py for the trade-offs.
    state_validate_on_read: bool = True

    # ─── redis ───────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_prefix: str = "spindle:queue"
    redis_consumer_group: str = "dispatchers"

    # ─── s3 / minio ──────────────────────────────────────────────────
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "spindle-artifacts"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"

    # ─── logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
