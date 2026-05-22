"""TTS base class and shared types.

Every backend's output is normalized to 24 kHz mono 16-bit WAV bytes so the
audio stitching / mp3-encoding stage downstream consumes a uniform format
regardless of which backend produced it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


SAMPLE_RATE = 24_000


@dataclass(frozen=True)
class Voice:
    """A voice exposed by a TTS backend.

    ``id`` is the provider-specific identifier callers pass back into
    ``BaseTTS.synthesize``. Other fields are presentation-only.
    """

    id: str
    name: str
    language: str | None = None
    description: str | None = None


class BaseTTS(ABC):
    """Pluggable text-to-speech backend.

    Implementations should:

    - Accept arbitrarily long ``text`` and chunk internally if the backend has
      a per-request length limit.
    - Return mono 16-bit PCM WAV bytes at ``sample_rate`` Hz.
    - Treat ``voice=None`` as "use the backend's default voice."
    - Accept implementation-specific extras via ``**opts``.
    """

    sample_rate: int = SAMPLE_RATE

    @abstractmethod
    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        """Render ``text`` as mono 16-bit WAV bytes at ``self.sample_rate`` Hz."""

    @abstractmethod
    def list_voices(self) -> list[Voice]:
        """Return the voices this backend exposes."""
