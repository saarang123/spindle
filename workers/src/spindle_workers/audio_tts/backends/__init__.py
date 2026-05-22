"""TTS backends — pluggable text-to-speech implementations.

v0 ships only ``openai``. ``f5`` and ``kokoro`` follow once needed (the
code already exists in podcast-this/cli/podcast/tts/; migration is mechanical).
"""
from .base import SAMPLE_RATE, BaseTTS, Voice
from .openai import OpenAITTS

__all__ = ["BaseTTS", "OpenAITTS", "SAMPLE_RATE", "Voice"]
