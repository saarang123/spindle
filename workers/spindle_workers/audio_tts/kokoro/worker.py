"""Kokoro-82M TTS worker — local, ~82M params, CPU-viable (GPU faster)."""
from __future__ import annotations

from ..backends.base import BaseTTS
from ..backends.kokoro import KokoroTTS
from ..worker import AudioTtsWorker


class KokoroTtsWorker(AudioTtsWorker):
    backend_name = "kokoro"

    def _make_backend(self) -> BaseTTS:
        return KokoroTTS()
