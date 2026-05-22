"""Boot the OpenAI TTS worker.

Invoked by the runtime supervisor as:
    module: spindle_workers.audio_tts.openai
"""
import asyncio

from .worker import OpenAITtsWorker


def main() -> None:
    asyncio.run(OpenAITtsWorker.boot())


if __name__ == "__main__":
    main()
