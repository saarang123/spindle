"""Tests for ChildProcess.

Most of the lifecycle requires a real subprocess; full e2e tests live in
test_supervisor. Here we cover the cheap, deterministic bits.
"""
from pathlib import Path

from spindle_runtime.child import ChildProcess
from spindle_runtime.config import RestartPolicy


def test_env_for_child_injects_reserved_keys(tmp_path: Path) -> None:
    c = ChildProcess(
        name="w",
        worker_id="w-0",
        module="spindle_workers.cpu_echo",
        env={"FOO": "bar"},
        logs_dir=tmp_path,
        restart=RestartPolicy(),
    )
    env = c._env_for_child()
    assert env["SPINDLE_WORKER_ID"] == "w-0"
    assert env["SPINDLE_WORKER_IPC_SOCKET"].endswith("spindle-worker-w-0.sock")
    assert env["SPINDLE_LOGS_DIR"] == str(tmp_path)
    assert env["FOO"] == "bar"


def test_snapshot_before_start(tmp_path: Path) -> None:
    c = ChildProcess(
        name="w",
        worker_id="w-0",
        module="spindle_workers.cpu_echo",
        env={},
        logs_dir=tmp_path,
        restart=RestartPolicy(),
    )
    snap = c.snapshot()
    assert snap.worker_id == "w-0"
    assert snap.is_running is False
    assert snap.pid is None
    assert snap.restart_count == 0
