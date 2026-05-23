"""TextRewriteWorker + backend tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from spindle_workers.base import WorkerConfig


def _set_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPINDLE_WORKER_ID", "rewrite-test-0")
    monkeypatch.setenv("SPINDLE_WORKER_CONFIG_ID", "text-rewrite-claude-v1")
    monkeypatch.setenv("SPINDLE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SPINDLE_WORKER_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")


def test_base_class_dataclass_shape() -> None:
    from spindle_workers.text_rewrite.backends import BaseRewriter, RewriteResult

    r = RewriteResult(text="hi", usage={"model": "x"})
    assert r.text == "hi"
    assert r.usage["model"] == "x"
    # BaseRewriter is abstract — can't instantiate directly
    with pytest.raises(TypeError):
        BaseRewriter()  # type: ignore[abstract]


def test_claude_text_rewrite_worker_boots(monkeypatch, tmp_path: Path) -> None:
    pytest.importorskip(
        "anthropic",
        reason="anthropic SDK not installed (audio_tts_claude extra)",
    )
    from spindle_workers.text_rewrite.claude import ClaudeTextRewriteWorker

    _set_env(monkeypatch, tmp_path)
    cfg = WorkerConfig.from_env()
    worker = ClaudeTextRewriteWorker(cfg)
    assert worker.backend_name == "claude"
    assert worker.capabilities == ["text.rewrite"]


def test_text_rewrite_worker_requires_backend_name_subclass(monkeypatch, tmp_path: Path) -> None:
    from spindle_workers.text_rewrite.worker import TextRewriteWorker

    _set_env(monkeypatch, tmp_path)
    cfg = WorkerConfig.from_env()

    class _Bad(TextRewriteWorker):
        def _make_backend(self):
            raise NotImplementedError

    with pytest.raises(SystemExit, match="backend_name is empty"):
        _Bad(cfg)
