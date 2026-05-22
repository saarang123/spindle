"""OpenAI TTS backend.

Wraps the OpenAI Audio Speech API. The API caps a single request at ~4096
characters; longer text is split at sentence boundaries via ``chunk_text``
and the WAV blobs are concatenated.
"""
from __future__ import annotations

import os

from openai import OpenAI

from ._util import chunk_text, concat_wav
from .base import BaseTTS, Voice


_VOICES: list[Voice] = [
    Voice(id="alloy", name="Alloy"),
    Voice(id="echo", name="Echo"),
    Voice(id="fable", name="Fable"),
    Voice(
        id="onyx",
        name="Onyx",
        description="Neutral male; well-suited to technical narration.",
    ),
    Voice(id="nova", name="Nova"),
    Voice(id="shimmer", name="Shimmer"),
]

_DEFAULT_VOICE = "onyx"
_MAX_CHARS_PER_REQUEST = 4000  # API limit is 4096; leave headroom


class OpenAITTS(BaseTTS):
    """OpenAI hosted TTS.

    ``model`` options:
      - ``tts-1``:    faster, lower fidelity.
      - ``tts-1-hd``: higher fidelity, slower (default).
    """

    sample_rate = 24_000

    def __init__(self, model: str = "tts-1-hd", api_key: str | None = None) -> None:
        self.model = model
        self._client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    def list_voices(self) -> list[Voice]:
        return list(_VOICES)

    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        voice_id = voice or _DEFAULT_VOICE
        speed = float(opts.get("speed", 1.0))
        chunks = chunk_text(text, _MAX_CHARS_PER_REQUEST)
        wav_blobs = [self._synthesize_chunk(c, voice_id, speed) for c in chunks]
        return concat_wav(wav_blobs)

    def _synthesize_chunk(self, text: str, voice_id: str, speed: float) -> bytes:
        response = self._client.audio.speech.create(
            model=self.model,
            voice=voice_id,
            input=text,
            response_format="wav",
            speed=speed,
        )
        return response.content
