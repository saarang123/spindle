"""``AudioTtsWorker`` — abstract base for backend-specific TTS workers.

Per Spindle's "one worker module per runtime backend" convention, each TTS
backend has its own concrete subclass and its own entry point:

  python -m spindle_workers.audio_tts.openai → OpenAITtsWorker
  python -m spindle_workers.audio_tts.kokoro → KokoroTtsWorker
  python -m spindle_workers.audio_tts.f5     → F5TtsWorker (deferred)

Subclasses set ``backend_name`` and implement ``_make_backend()``. The base
class owns the shared job-execution path (input parsing, synthesize call,
output shape) so every TTS backend produces uniform results.

Different backends often need different deployment envs (CPU vs GPU,
incompatible torch versions, different Python versions). Use
``WorkerSpec.python`` in the runtime YAML to point each one at its own venv.
"""
from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod

from spindle_core.types.job import Job

from ..base import JobContext, JobResult, WorkerBase, WorkerConfig
from .backends.base import BaseTTS

log = logging.getLogger("spindle_workers")


class AudioTtsWorker(WorkerBase):
    """Abstract base for TTS workers. Subclass and override
    ``backend_name`` + ``_make_backend()``.
    """

    capabilities = ["audio.tts"]
    backend_name: str = ""  # concrete subclass MUST set

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)
        if not self.backend_name:
            raise SystemExit(
                f"{type(self).__name__}.backend_name is empty. Concrete "
                f"subclass must set it (e.g. 'openai', 'kokoro')."
            )
        self._tts = self._make_backend()
        log.info("audio_tts backend=%s ready", self.backend_name)

    @abstractmethod
    def _make_backend(self) -> BaseTTS:
        """Return the BaseTTS implementation for this subclass."""

    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        from spindle_core.types.artifact import ArtifactKind
        from .backends._util import wav_duration_seconds

        text = job.input["text"]
        voice = job.input.get("voice")
        options = job.input.get("options") or {}

        log.info("synth start chars=%d voice=%s", len(text), voice or "default")
        # Synthesize off the event loop so heartbeats / IPC stay responsive.
        wav_bytes = await asyncio.to_thread(
            self._tts.synthesize, text, voice, **options
        )
        duration_s = wav_duration_seconds(wav_bytes)
        log.info(
            "synth done bytes=%d duration_s=%.2f",
            len(wav_bytes), duration_s,
        )

        await ctx.artifacts.write(
            key="audio.wav",
            data=wav_bytes,
            kind=ArtifactKind.AUDIO,
            mime_type="audio/wav",
            duration_seconds=duration_s,
            metadata={
                "sample_rate": self._tts.sample_rate,
                "channels": 1,
                "backend": self.backend_name,
                "voice": voice or "default",
            },
        )

        return JobResult(
            output={
                "voice": voice or "default",
                "backend": self.backend_name,
                "char_count": len(text),
                "duration_seconds": duration_s,
            },
            runtime={
                "sample_rate": self._tts.sample_rate,
                "wav_bytes": len(wav_bytes),
            },
        )
