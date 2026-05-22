"""AudioTtsWorker tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from spindle_workers.audio_tts.backends._util import (
    chunk_text,
    concat_wav,
    wav_duration_seconds,
)
from spindle_workers.audio_tts.backends.base import SAMPLE_RATE, BaseTTS, Voice
from spindle_workers.audio_tts.backends.openai import OpenAITTS
from spindle_workers.audio_tts.worker import AudioTtsWorker
from spindle_workers.base import WorkerConfig


def _set_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPINDLE_WORKER_ID", "tts-test-0")
    monkeypatch.setenv("SPINDLE_WORKER_CONFIG_ID", "audio-tts-openai-v1")
    monkeypatch.setenv("SPINDLE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SPINDLE_WORKER_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-for-tests")


def test_base_class_defaults() -> None:
    assert SAMPLE_RATE == 24_000
    v = Voice(id="alloy", name="Alloy")
    assert v.id == "alloy"


def test_openai_tts_lists_voices_without_api_call() -> None:
    tts = OpenAITTS(api_key="sk-fake-no-network")
    voices = tts.list_voices()
    assert all(isinstance(v, Voice) for v in voices)
    assert any(v.id == "onyx" for v in voices)
    assert tts.sample_rate == 24_000


def test_chunk_text_short_passthrough() -> None:
    assert chunk_text("hello", 100) == ["hello"]


def test_chunk_text_splits_at_sentence_boundaries() -> None:
    text = "Sentence one. Sentence two. Sentence three. Sentence four."
    chunks = chunk_text(text, 30)
    assert len(chunks) >= 2
    assert all(len(c) <= 30 for c in chunks)


def test_chunk_text_hard_splits_oversized_sentences() -> None:
    huge = "a" * 100
    chunks = chunk_text(huge, 30)
    assert len(chunks) == 4
    assert all(len(c) <= 30 for c in chunks)


def test_wav_duration_seconds_zero_on_empty() -> None:
    assert wav_duration_seconds(b"") == 0.0


def test_concat_wav_empty_list() -> None:
    assert concat_wav([]) == b""


def test_concat_wav_single_passthrough() -> None:
    payload = b"RIFF....WAVEfmt "
    assert concat_wav([payload]) == payload


def test_audio_tts_worker_requires_backend_env(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SPINDLE_TTS_BACKEND", raising=False)
    cfg = WorkerConfig.from_env()
    with pytest.raises(SystemExit, match="SPINDLE_TTS_BACKEND"):
        AudioTtsWorker(cfg)


def test_audio_tts_worker_loads_openai_backend(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SPINDLE_TTS_BACKEND", "openai")
    cfg = WorkerConfig.from_env()
    worker = AudioTtsWorker(cfg)
    assert worker._backend_name == "openai"
    assert isinstance(worker._tts, BaseTTS)
    assert worker.capabilities == ["audio.tts"]


def test_audio_tts_worker_unknown_backend_exits(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SPINDLE_TTS_BACKEND", "espeak")
    cfg = WorkerConfig.from_env()
    with pytest.raises(SystemExit, match="not built yet"):
        AudioTtsWorker(cfg)
