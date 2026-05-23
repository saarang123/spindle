"""F5-TTS backend (local; GPU recommended).

F5-TTS is voice-cloning-first: every synthesis needs a reference audio clip
plus its transcript. Default voice resolution order:

  1. Explicit ``default_ref_audio`` + ``default_ref_text`` to the constructor
  2. ``SPINDLE_F5_REF_AUDIO`` + ``SPINDLE_F5_REF_TEXT`` env vars
  3. F5-TTS's bundled sample reference (``f5_tts/infer/examples/basic/``)

Pass ``voice="/path/to/ref.wav"`` per-call (with ``ref_text=...`` or a sidecar
``<same-stem>.txt`` transcript) to use a different voice on a single job.

Requires the optional ``[audio_tts_f5]`` install (pulls torch + f5-tts).
"""
from __future__ import annotations

import os
from pathlib import Path

from ._util import samples_to_wav
from .base import BaseTTS, Voice


_BUNDLED_REF_TEXT = "Some call me nature, others call me mother nature."


class F5TTS(BaseTTS):
    """F5-TTS local synthesis.

    Args:
        model_name: F5-TTS variant. Must match a config file shipped with
            f5-tts under ``f5_tts/configs/{name}.yaml``. Defaults to
            ``"F5TTS_v1_Base"`` (the v1.1.x recommended base model).
            Other options include ``"F5TTS_Base"``, ``"F5TTS_Small"``,
            ``"F5TTS_v1_Small"``, ``"E2TTS_Base"``, ``"E2TTS_Small"``.
        device: torch device string (``"cuda"``, ``"cpu"``, ``"mps"``).
            ``None`` lets f5-tts pick.
        default_ref_audio: path to the reference wav for the default voice.
            Falls back to ``SPINDLE_F5_REF_AUDIO``, then the bundled sample.
        default_ref_text: transcript of the reference audio. Falls back to
            ``SPINDLE_F5_REF_TEXT``, then the bundled transcript.
    """

    sample_rate = 24_000

    def __init__(
        self,
        model_name: str = "F5TTS_v1_Base",
        device: str | None = None,
        default_ref_audio: str | Path | None = None,
        default_ref_text: str | None = None,
    ) -> None:
        from f5_tts.api import F5TTS as F5TTSCore  # type: ignore

        kwargs: dict = {"model": model_name}
        if device is not None:
            kwargs["device"] = device
        self._core = F5TTSCore(**kwargs)

        ref_audio = default_ref_audio or os.environ.get("SPINDLE_F5_REF_AUDIO")
        ref_text = default_ref_text or os.environ.get("SPINDLE_F5_REF_TEXT")

        if not ref_audio:
            # Fall back to F5-TTS's bundled sample reference. Resolve via
            # importlib.resources because f5_tts is a namespace package
            # (no top-level __init__.py → f5_tts.__file__ is None).
            try:
                from importlib.resources import files

                bundled = files("f5_tts") / "infer" / "examples" / "basic" / "basic_ref_en.wav"
                # files() returns a Traversable; materialize as a real path.
                # On wheel installs it's already a regular filesystem path.
                bundled_path = Path(str(bundled))
                if bundled_path.exists():
                    ref_audio = bundled_path
                    ref_text = ref_text or _BUNDLED_REF_TEXT
            except (ModuleNotFoundError, FileNotFoundError):
                pass

        self._default_ref_audio = Path(ref_audio) if ref_audio else None
        self._default_ref_text = ref_text

    def list_voices(self) -> list[Voice]:
        if self._default_ref_audio:
            return [
                Voice(
                    id="default",
                    name=f"Default ({self._default_ref_audio.name})",
                    description="Configured default reference voice.",
                )
            ]
        return []

    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        ref_audio, ref_text = self._resolve_reference(voice, opts)
        wav_samples, sr, _ = self._core.infer(
            ref_file=str(ref_audio),
            ref_text=ref_text,
            gen_text=text,
        )
        return samples_to_wav(wav_samples, sr)

    def _resolve_reference(
        self, voice: str | None, opts: dict
    ) -> tuple[Path, str]:
        if voice is None or voice == "default":
            if not self._default_ref_audio or not self._default_ref_text:
                raise ValueError(
                    "F5TTS has no default voice configured. Set "
                    "SPINDLE_F5_REF_AUDIO + SPINDLE_F5_REF_TEXT env vars, "
                    "or pass voice='/path/to/ref.wav' with ref_text=... "
                    "per synthesize call."
                )
            return self._default_ref_audio, self._default_ref_text

        ref_audio = Path(voice)
        if not ref_audio.exists():
            raise FileNotFoundError(f"Reference audio not found: {ref_audio}")
        ref_text = opts.get("ref_text")
        if ref_text is None:
            sidecar = ref_audio.with_suffix(".txt")
            if not sidecar.exists():
                raise ValueError(
                    f"No transcript provided for {ref_audio}. Pass ref_text= "
                    f"or place a transcript at {sidecar}."
                )
            ref_text = sidecar.read_text(encoding="utf-8").strip()
        return ref_audio, ref_text
