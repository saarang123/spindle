"""WorkerBase tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from spindle_workers.base import JobContext, JobResult, WorkerBase, WorkerConfig


def _set_required_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPINDLE_WORKER_ID", "test-worker-0")
    monkeypatch.setenv("SPINDLE_WORKER_CONFIG_ID", "test-config-v1")
    monkeypatch.setenv("SPINDLE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SPINDLE_WORKER_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("SPINDLE_WORKER_HEARTBEAT_SECONDS", "0.05")


def test_worker_config_from_env_requires_keys(monkeypatch) -> None:
    monkeypatch.delenv("SPINDLE_WORKER_ID", raising=False)
    with pytest.raises(SystemExit, match="SPINDLE_WORKER_ID"):
        WorkerConfig.from_env()


def test_worker_config_from_env_populates(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)
    cfg = WorkerConfig.from_env()
    assert cfg.worker_id == "test-worker-0"
    assert cfg.config_id == "test-config-v1"
    assert cfg.logs_dir == tmp_path / "logs"
    assert cfg.heartbeat_seconds == 0.05


class _NoopWorker(WorkerBase):
    capabilities = ["test.noop"]

    async def execute(self, job, ctx):  # type: ignore[override]
        return JobResult(output={"ok": True})


async def test_worker_register_writes_descriptor(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)
    cfg = WorkerConfig.from_env()
    worker = _NoopWorker(cfg)

    await worker._register()

    desc_path = cfg.registry_dir / f"{cfg.worker_id}.json"
    assert desc_path.exists()
    desc = json.loads(desc_path.read_text())
    assert desc["worker_id"] == "test-worker-0"
    assert desc["config_id"] == "test-config-v1"
    assert desc["capabilities"] == ["test.noop"]
    assert desc["ipc_socket"].endswith("spindle-worker-test-worker-0.sock")

    await worker._unregister()
    assert not desc_path.exists()


async def test_worker_run_shuts_down_cleanly(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)
    cfg = WorkerConfig.from_env()
    worker = _NoopWorker(cfg)

    task = asyncio.create_task(worker.run())
    # Let the worker register + emit a heartbeat
    await asyncio.sleep(0.15)
    worker._shutdown_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    desc_path = cfg.registry_dir / f"{cfg.worker_id}.json"
    assert not desc_path.exists()
