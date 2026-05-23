"""Dispatcher unit tests.

Full tick → lease → IPC round-trip lives in the end-to-end smoke test
(requires Mongo + Redis + MinIO). These tests cover the cheap pieces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spindle_runtime.child import ChildProcess
from spindle_runtime.config import RestartPolicy
from spindle_runtime.dispatcher import Dispatcher


def _make_child(name: str, worker_id: str, config_id: str | None, tmp_path: Path) -> ChildProcess:
    return ChildProcess(
        name=name,
        worker_id=worker_id,
        module="spindle_workers.audio_tts.openai",
        env={},
        logs_dir=tmp_path,
        restart=RestartPolicy(),
        config_id=config_id,
    )


def test_dispatcher_active_configs_derives_from_children(tmp_path: Path) -> None:
    children = [
        _make_child("openai", "openai-0", "audio-tts-openai-v1", tmp_path),
        _make_child("openai", "openai-1", "audio-tts-openai-v1", tmp_path),
        _make_child("kokoro", "kokoro-0", "audio-tts-kokoro-v1", tmp_path),
        _make_child("orphan", "orphan-0", None, tmp_path),
    ]
    d = Dispatcher(
        node_id="test",
        children=children,
        state=object(),  # not exercised in this test
        queue=object(),
    )
    assert d.active_configs() == [
        "audio-tts-kokoro-v1",
        "audio-tts-openai-v1",
    ]


def test_dispatcher_explicit_configs_override(tmp_path: Path) -> None:
    children = [
        _make_child("openai", "openai-0", "audio-tts-openai-v1", tmp_path),
    ]
    d = Dispatcher(
        node_id="test",
        children=children,
        state=object(),
        queue=object(),
        configs_override=["only-this-one"],
    )
    assert d.active_configs() == ["only-this-one"]


def test_dispatcher_picks_only_alive_local_workers(tmp_path: Path) -> None:
    """_pick_local_worker should skip children whose proc isn't alive yet."""
    children = [
        _make_child("openai", "openai-0", "audio-tts-openai-v1", tmp_path),
    ]
    d = Dispatcher(
        node_id="test",
        children=children,
        state=object(),
        queue=object(),
    )
    # Before .run() the child has no proc → is_alive() == False
    assert d._pick_local_worker("audio-tts-openai-v1") is None

    # Wrong config_id → None too
    assert d._pick_local_worker("nonexistent") is None
    assert d._pick_local_worker(None) is None
