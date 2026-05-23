"""F5-TTS worker subpackage.

Entry point: ``python -m spindle_workers.audio_tts.f5``.

Requires the ``[audio_tts_f5]`` extra (torch + f5-tts). GPU strongly
recommended — CPU inference is single-digit RTF and not practical at
podcast lengths. Use ``WorkerSpec.python`` in the runtime YAML to pin a
dedicated venv if the rest of the machine runs newer Python.
"""
from .worker import F5TtsWorker

__all__ = ["F5TtsWorker"]
