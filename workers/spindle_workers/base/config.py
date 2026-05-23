"""Per-worker config read from env vars at process start.

The runtime supervisor injects ``SPINDLE_WORKER_ID``,
``SPINDLE_WORKER_IPC_SOCKET``, and ``SPINDLE_LOGS_DIR``. Other settings come
from the supervisor's YAML (passed via ``env:``) or defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkerConfig:
    worker_id: str
    config_id: str
    ipc_socket: Path
    logs_dir: Path
    api_url: str
    api_auth_token: str | None
    node: str
    heartbeat_seconds: float
    registry_dir: Path
    concurrency_limit: int

    @classmethod
    def from_env(cls) -> WorkerConfig:
        worker_id = _require_env("SPINDLE_WORKER_ID")
        config_id = _require_env("SPINDLE_WORKER_CONFIG_ID")

        ipc_socket = Path(
            os.environ.get(
                "SPINDLE_WORKER_IPC_SOCKET",
                f"/tmp/spindle-worker-{worker_id}.sock",
            )
        )
        logs_dir = Path(
            os.environ.get("SPINDLE_LOGS_DIR", "~/.spindle/logs")
        ).expanduser()
        api_url = os.environ.get("SPINDLE_API_URL", "http://localhost:8080")
        api_auth_token = os.environ.get("SPINDLE_API_AUTH_TOKEN") or None
        node = os.environ.get("SPINDLE_NODE", "control")
        heartbeat_seconds = float(
            os.environ.get("SPINDLE_WORKER_HEARTBEAT_SECONDS", "10")
        )
        registry_dir = Path(
            os.environ.get("SPINDLE_WORKER_REGISTRY_DIR", "/tmp/spindle-workers")
        )
        concurrency_limit = int(
            os.environ.get("SPINDLE_WORKER_CONCURRENCY_LIMIT", "1")
        )

        return cls(
            worker_id=worker_id,
            config_id=config_id,
            ipc_socket=ipc_socket,
            logs_dir=logs_dir,
            api_url=api_url,
            api_auth_token=api_auth_token,
            node=node,
            heartbeat_seconds=heartbeat_seconds,
            registry_dir=registry_dir,
            concurrency_limit=concurrency_limit,
        )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value
