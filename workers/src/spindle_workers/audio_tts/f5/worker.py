"""F5-TTS worker — local, voice-cloning-first."""
from __future__ import annotations

from ..backends.base import BaseTTS
from ..backends.f5 import F5TTS
from ..worker import AudioTtsWorker


class F5TtsWorker(AudioTtsWorker):
    backend_name = "f5"

    def _make_backend(self) -> BaseTTS:
        # Reads SPINDLE_F5_REF_AUDIO / SPINDLE_F5_REF_TEXT env vars (or falls
        # back to F5-TTS's bundled sample reference if neither set).
        return F5TTS()
