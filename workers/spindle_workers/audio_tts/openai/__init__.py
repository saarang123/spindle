"""OpenAI TTS worker subpackage.

Entry point: ``python -m spindle_workers.audio_tts.openai``.
"""
from .worker import OpenAITtsWorker

__all__ = ["OpenAITtsWorker"]
