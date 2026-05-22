"""``AudioTtsWorker`` — first real (non-echo) worker.

Synthesizes ``job.input.text`` to a 24 kHz mono WAV via the backend selected
by ``SPINDLE_TTS_BACKEND``. v0 ships the ``openai`` backend only; ``f5`` and
``kokoro`` arrive in a later phase (the backends are already specced and the
code already exists in ``podcast-this/cli/podcast/tts/`` — just not migrated
here yet).
"""
from __future__ import annotations

import logging
import os

from spindle_core.types.job import Job

from ..base import JobContext, JobResult, WorkerBase, WorkerConfig
from .backends.base import BaseTTS

log = logging.getLogger("spindle_workers")


class AudioTtsWorker(WorkerBase):
    capabilities = ["audio.tts"]

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)
        backend_name = os.environ.get("SPINDLE_TTS_BACKEND")
        if not backend_name:
            raise SystemExit(
                "SPINDLE_TTS_BACKEND is required (one of: openai)"
            )
        self._backend_name = backend_name
        self._tts: BaseTTS = self._load_backend(backend_name)
        log.info("audio_tts backend=%s ready", backend_name)

    @staticmethod
    def _load_backend(name: str) -> BaseTTS:
        if name == "openai":
            from .backends.openai import OpenAITTS

            return OpenAITTS()
        # f5 / kokoro deferred until their migration from podcast-this.
        raise SystemExit(
            f"backend {name!r} not built yet in spindle-workers v0; "
            f"only 'openai' is available."
        )

    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        text = job.input["text"]
        voice = job.input.get("voice")
        options = job.input.get("options") or {}

        log.info("synth start chars=%d voice=%s", len(text), voice or "default")
        wav_bytes = self._tts.synthesize(text, voice, **options)
        log.info("synth done bytes=%d", len(wav_bytes))

        # v0: no ArtifactWriter yet — return byte count + sample-rate metadata.
        # When ArtifactWriter lands, this method writes WAV to MinIO and returns
        # an ArtifactMeta in `artifacts`.
        return JobResult(
            output={
                "voice": voice or "default",
                "backend": self._backend_name,
                "char_count": len(text),
                "wav_bytes": len(wav_bytes),
            },
            runtime={"sample_rate": self._tts.sample_rate},
        )
