"""Tests for the runtime YAML config schema."""
from pathlib import Path

import pytest

from spindle_runtime.config import (
    RESERVED_ENV_KEYS,
    RestartPolicy,
    WorkerSpec,
    WorkersConfig,
    resolve_logs_dir,
)


def test_worker_spec_defaults() -> None:
    spec = WorkerSpec(name="w", module="m")
    assert spec.replicas == 1
    assert spec.env == {}
    assert spec.restart.policy == "on_failure"
    assert spec.restart.backoff_s[0] == 1.0


def test_worker_spec_replicas_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WorkerSpec(name="w", module="m", replicas=0)


@pytest.mark.parametrize("key", sorted(RESERVED_ENV_KEYS))
def test_worker_spec_rejects_reserved_env_keys(key: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        WorkerSpec(name="w", module="m", env={key: "x"})


def test_resolve_logs_dir_precedence(tmp_path: Path) -> None:
    cli = tmp_path / "from-cli"
    env_dir = tmp_path / "from-env"
    yaml_dir = tmp_path / "from-yaml"

    assert resolve_logs_dir(cli, str(env_dir), yaml_dir) == cli
    assert resolve_logs_dir(None, str(env_dir), yaml_dir) == env_dir
    assert resolve_logs_dir(None, None, yaml_dir) == yaml_dir
    fallback = resolve_logs_dir(None, None, None)
    assert fallback.is_absolute()


def test_workers_config_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
node_id: test
log_dir: /tmp/spindle-test-logs
shutdown_grace_seconds: 5
workers:
  - name: w1
    module: spindle_workers.cpu_echo
    replicas: 2
  - name: w2
    module: spindle_workers.audio_tts
    env:
      SPINDLE_TTS_BACKEND: openai
      SPINDLE_WORKER_CONFIG_ID: audio-tts-openai-v1
    restart:
      policy: always
      backoff_s: [0.5, 1.0]
"""
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml_text)
    cfg = WorkersConfig.from_yaml(p)

    assert cfg.node_id == "test"
    assert cfg.shutdown_grace_seconds == 5
    assert len(cfg.workers) == 2
    assert cfg.workers[0].replicas == 2
    assert cfg.workers[1].env["SPINDLE_TTS_BACKEND"] == "openai"
    assert cfg.workers[1].restart.policy == "always"
    assert cfg.workers[1].restart.backoff_s == [0.5, 1.0]
