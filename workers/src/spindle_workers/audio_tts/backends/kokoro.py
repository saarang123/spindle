"""Kokoro-82M backend (local; CPU-viable, GPU faster).

Kokoro is a compact (~82M param) open-source TTS model with bundled preset
voices. No voice cloning — pick a voice id from ``list_voices()``.

Requires the optional ``[audio_tts_kokoro]`` install. API surface targets
``kokoro >= 0.7``.

Deployment note: kokoro's transitive ``spacy`` dependency only ships wheels
for Python 3.12 and 3.13. On 3.14 the install will fail; use ``WorkerSpec
.python`` to point this worker at a dedicated Python 3.13 venv when the rest
of the machine runs newer Python.
"""
from __future__ import annotations

from ._util import samples_to_wav
from .base import BaseTTS, Voice


# Subset of Kokoro v1.0 English voices that are well-tested.
_VOICES: list[Voice] = [
    Voice(id="af_bella", name="Bella", language="en-US", description="American female."),
    Voice(id="af_sarah", name="Sarah", language="en-US", description="American female."),
    Voice(id="am_adam", name="Adam", language="en-US", description="American male."),
    Voice(
        id="am_michael",
        name="Michael",
        language="en-US",
        description="American male; neutral, good for narration.",
    ),
    Voice(id="bf_emma", name="Emma", language="en-GB", description="British female."),
    Voice(id="bf_isabella", name="Isabella", language="en-GB", description="British female."),
    Voice(id="bm_george", name="George", language="en-GB", description="British male."),
    Voice(id="bm_lewis", name="Lewis", language="en-GB", description="British male."),
]

_DEFAULT_VOICE = "am_michael"


class KokoroTTS(BaseTTS):
    """Kokoro-82M local synthesis.

    Args:
        lang_code: Kokoro language code. ``"a"`` = American English (default),
            ``"b"`` = British English. Match this to the voices you intend to
            use (voices are prefixed ``a*`` or ``b*``).
    """

    sample_rate = 24_000

    def __init__(self, lang_code: str = "a") -> None:
        from kokoro import KPipeline  # type: ignore

        self._pipeline = KPipeline(lang_code=lang_code)

    def list_voices(self) -> list[Voice]:
        return list(_VOICES)

    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        import numpy as np

        voice_id = voice or _DEFAULT_VOICE
        # KPipeline yields (graphemes, phonemes, audio) per internal chunk.
        chunks = [audio for _, _, audio in self._pipeline(text, voice=voice_id)]
        if not chunks:
            return samples_to_wav(np.zeros(0, dtype=np.float32), self.sample_rate)
        combined = np.concatenate(chunks)
        return samples_to_wav(combined, self.sample_rate)
