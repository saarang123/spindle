"""Entry point for the audio_tts worker.

Two modes:

  python -m spindle_workers.audio_tts
      Boot as a long-running worker. Registers, heartbeats, waits for SIGTERM.
      The runtime supervisor calls this form. Requires:
        SPINDLE_WORKER_ID
        SPINDLE_WORKER_CONFIG_ID
        SPINDLE_TTS_BACKEND  (openai)
        OPENAI_API_KEY       (for openai backend)

  python -m spindle_workers.audio_tts --test "some text" [--voice onyx] [--out test.wav]
      One-shot synthesis without API / dispatcher. Writes WAV to ``--out``.
      Useful for validating the backend works.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .worker import AudioTtsWorker


def _build_test_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m spindle_workers.audio_tts",
        description="Audio TTS worker. Default = boot as worker; --test = one-shot.",
    )
    p.add_argument("--test", metavar="TEXT", help="One-shot synthesis of TEXT.")
    p.add_argument("--voice", default="onyx", help="Voice id (default: onyx).")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("./test-output.wav"),
        help="Output WAV path for --test mode.",
    )
    p.add_argument(
        "--backend",
        default="openai",
        help="Backend for --test mode (default: openai).",
    )
    return p


def _run_test(args: argparse.Namespace) -> int:
    if args.backend != "openai":
        sys.stderr.write(
            f"--test only supports backend=openai in v0 (got {args.backend!r})\n"
        )
        return 2

    from .backends.openai import OpenAITTS

    tts = OpenAITTS()
    wav = tts.synthesize(args.test, voice=args.voice)
    args.out.write_bytes(wav)
    print(f"wrote {len(wav)} bytes ({len(args.test)} chars) → {args.out}")
    return 0


def main() -> None:
    # Special-case --test so the default no-args path stays simple (boot worker).
    if "--test" in sys.argv:
        args = _build_test_parser().parse_args()
        sys.exit(_run_test(args))

    asyncio.run(AudioTtsWorker.boot())


if __name__ == "__main__":
    main()
