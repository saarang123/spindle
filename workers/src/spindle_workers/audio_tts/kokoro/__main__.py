"""Boot the Kokoro TTS worker.

Invoked by the runtime supervisor as:
    module: spindle_workers.audio_tts.kokoro
"""
import asyncio

from .worker import KokoroTtsWorker


def main() -> None:
    asyncio.run(KokoroTtsWorker.boot())


if __name__ == "__main__":
    main()
