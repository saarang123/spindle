"""Kokoro-82M TTS worker subpackage.

Entry point: ``python -m spindle_workers.audio_tts.kokoro``.

Requires the ``[audio_tts_kokoro]`` extra and Python 3.12 or 3.13 (kokoro's
transitive spacy dep doesn't ship 3.14 wheels). Use ``WorkerSpec.python`` in
the runtime YAML to pin a dedicated venv when the rest of the machine runs
newer Python.
"""
from .worker import KokoroTtsWorker

__all__ = ["KokoroTtsWorker"]
