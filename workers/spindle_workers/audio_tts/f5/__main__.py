"""Boot the F5-TTS worker.

Invoked by the runtime supervisor as:
    module: spindle_workers.audio_tts.f5
"""
import asyncio

from .worker import F5TtsWorker


def main() -> None:
    asyncio.run(F5TtsWorker.boot())


if __name__ == "__main__":
    main()
