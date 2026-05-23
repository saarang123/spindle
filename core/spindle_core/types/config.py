"""ModelConfig — a named runtime configuration for a model.

Jobs reference a config by `config_id` at submission time. The dispatcher reads
configs to learn which run on which node. The worker reads its own config at
boot to know which model to load.

Storage:
  - Mongo collection `model_configs`. `_id` is the human-readable `id` (string),
    e.g., "qwen-text-v1" — not a UUID.
  - YAML files in `configs/` will be the future source of truth, synced into
    Mongo via `spindle config apply`. For now, seed manually.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spindle_core._time import utcnow


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str  # human-readable, stable, e.g. "qwen-text-v1"
    name: str
    version: str

    # capabilities — which job types this config can serve.
    job_types: list[str]

    # routing
    preferred_node: str | None = None  # None = any node
    runtime_backend: str  # "mlx", "diffusers", "comfyui", "ffmpeg", "openai"
    model_ref: str  # backend-specific model identifier

    # tunables passed to the worker at execution time
    params: dict[str, Any] = Field(default_factory=dict)
    resource_requirements: dict[str, Any] = Field(default_factory=dict)

    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
