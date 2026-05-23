"""OpenAI TTS worker — hosted, CPU-only, no GPU required.

Boots fastest of the TTS backends (just an HTTP client). Replicate this
worker (``replicas: 4`` etc.) to multiply throughput at the per-process
concurrency limit.
"""
from __future__ import annotations

from ..backends.base import BaseTTS
from ..backends.openai import OpenAITTS
from ..worker import AudioTtsWorker


class OpenAITtsWorker(AudioTtsWorker):
    backend_name = "openai"

    def _make_backend(self) -> BaseTTS:
        return OpenAITTS()
