"""YAML config schema for the runtime supervisor.

One file per machine. The supervisor reads only that file and treats every
entry under ``workers`` as a process to spawn (multiplied by ``replicas``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


RESERVED_ENV_KEYS = frozenset({
    "SPINDLE_WORKER_ID",
    "SPINDLE_WORKER_IPC_SOCKET",
    "SPINDLE_LOGS_DIR",
})


class RestartPolicy(BaseModel):
    """How the supervisor responds when a child exits."""

    policy: Literal["always", "on_failure", "never"] = "on_failure"
    backoff_s: list[float] = Field(default_factory=lambda: [1.0, 2.0, 4.0, 8.0, 30.0])
    max_consecutive_failures: int = 0  # 0 = unlimited


class WorkerSpec(BaseModel):
    """One worker entry from YAML. Becomes N child processes when ``replicas > 1``."""

    name: str
    module: str
    replicas: int = Field(default=1, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    restart: RestartPolicy = Field(default_factory=RestartPolicy)
    python: str | None = None  # Override interpreter. Path to a Python executable
                                # (e.g. ~/envs/f5-tts/bin/python or a conda env's
                                # python). None = use the supervisor's interpreter.

    @model_validator(mode="after")
    def _no_reserved_env(self) -> WorkerSpec:
        leaked = RESERVED_ENV_KEYS & self.env.keys()
        if leaked:
            raise ValueError(
                f"worker {self.name!r}: env must not include reserved keys "
                f"(supervisor owns these): {sorted(leaked)}"
            )
        return self


class DispatcherConfig(BaseModel):
    """Embedded-dispatcher settings.

    If this block is present in YAML, the runtime starts a dispatcher task
    alongside the supervisor. If absent, the runtime is supervisor-only
    (workers boot but no one routes jobs to them — useful for testing).
    """

    lease_ttl_seconds: float = 300.0
    tick_block_ms: int = 200
    # Optional explicit config list. If empty/None, derived from local workers'
    # SPINDLE_WORKER_CONFIG_ID env values.
    configs: list[str] = Field(default_factory=list)


class WorkersConfig(BaseModel):
    """The entire runtime config for one machine."""

    node_id: str
    log_dir: Path = Field(default_factory=lambda: Path("~/.spindle/logs"))
    shutdown_grace_seconds: float = 10.0
    workers: list[WorkerSpec]
    dispatcher: DispatcherConfig | None = None

    @classmethod
    def from_yaml(cls, path: Path | str) -> WorkersConfig:
        path = Path(path).expanduser()
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)


def resolve_logs_dir(
    cli_flag: Path | None,
    env_value: str | None,
    yaml_value: Path | None,
    default: Path | None = None,
) -> Path:
    """Resolve logs_dir with precedence: CLI > env > YAML > default."""
    if cli_flag is not None:
        return Path(cli_flag).expanduser()
    if env_value:
        return Path(env_value).expanduser()
    if yaml_value is not None:
        return Path(yaml_value).expanduser()
    return Path(default or "~/.spindle/logs").expanduser()
