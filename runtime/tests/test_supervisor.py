"""Supervisor tests.

The cheap shape checks live here. Full subprocess e2e tests come later when
there are real worker modules to spawn.
"""
import os
from pathlib import Path

from spindle_runtime.config import (
    RestartPolicy,
    WorkerSpec,
    WorkersConfig,
)
from spindle_runtime.supervisor import Supervisor


def test_supervisor_builds_one_child_per_replica(tmp_path: Path) -> None:
    cfg = WorkersConfig(
        node_id="test",
        log_dir=tmp_path,
        workers=[
            WorkerSpec(
                name="tts-openai",
                module="spindle_workers.audio_tts",
                replicas=4,
                env={"SPINDLE_TTS_BACKEND": "openai"},
            ),
            WorkerSpec(
                name="tts-f5",
                module="spindle_workers.audio_tts",
                replicas=1,
                env={"SPINDLE_TTS_BACKEND": "f5"},
            ),
        ],
    )
    sup = Supervisor(cfg, logs_dir=tmp_path)
    assert len(sup.children) == 5

    worker_ids = [c.worker_id for c in sup.children]
    assert worker_ids[:4] == [f"tts-openai-{i}" for i in range(4)]
    assert worker_ids[4] == "tts-f5-0"


def test_supervisor_propagates_env_per_spec(tmp_path: Path) -> None:
    cfg = WorkersConfig(
        node_id="test",
        log_dir=tmp_path,
        workers=[
            WorkerSpec(
                name="w",
                module="m",
                env={"FOO": "bar"},
                restart=RestartPolicy(policy="always"),
            ),
        ],
    )
    sup = Supervisor(cfg, logs_dir=tmp_path)
    child_env = sup.children[0]._env_for_child()
    assert child_env["FOO"] == "bar"
    assert child_env["SPINDLE_WORKER_ID"] == "w-0"
    # parent env passes through too
    assert os.environ.get("PATH") is not None
    assert child_env.get("PATH") == os.environ.get("PATH")


def test_supervisor_uses_provided_logs_dir(tmp_path: Path) -> None:
    yaml_logs = tmp_path / "yaml-logs"
    explicit = tmp_path / "explicit-logs"
    cfg = WorkersConfig(
        node_id="test",
        log_dir=yaml_logs,
        workers=[WorkerSpec(name="w", module="m")],
    )
    sup = Supervisor(cfg, logs_dir=explicit)
    assert sup.logs_dir == explicit
    assert sup.children[0]._env_for_child()["SPINDLE_LOGS_DIR"] == str(explicit)
