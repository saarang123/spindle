"""One-shot ``--test`` CLI for the audio_tts backends.

This module is no longer the worker entry point — each TTS backend has its
own subpackage:

    python -m spindle_workers.audio_tts.openai   (boots OpenAITtsWorker)
    python -m spindle_workers.audio_tts.kokoro   (boots KokoroTtsWorker)

This file only handles the ``--test`` flag for local validation without
the supervisor / API / dispatcher:

    python -m spindle_workers.audio_tts --test "hello world" --backend openai
    python -m spindle_workers.audio_tts --test "hello world" --backend kokoro --voice am_michael --out ./test.wav
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m spindle_workers.audio_tts",
        description="One-shot TTS synthesis for local validation.",
    )
    p.add_argument(
        "--test",
        metavar="TEXT",
        required=True,
        help="Text to synthesize.",
    )
    p.add_argument(
        "--backend",
        default="openai",
        choices=("openai", "kokoro"),
        help="Backend (default: openai).",
    )
    p.add_argument(
        "--voice",
        default=None,
        help="Voice id (backend-specific default if omitted).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("./test-output.wav"),
        help="Output WAV path.",
    )
    return p


def _run_test(args: argparse.Namespace) -> int:
    if args.backend == "openai":
        from .backends.openai import OpenAITTS

        tts = OpenAITTS()
    elif args.backend == "kokoro":
        try:
            from .backends.kokoro import KokoroTTS
        except ImportError as e:
            sys.stderr.write(
                f"kokoro not installed: {e}\n"
                "Install with: uv sync --extra audio_tts_kokoro\n"
                "(kokoro requires Python 3.12 or 3.13 due to spacy wheels.)\n"
            )
            return 3
        tts = KokoroTTS()
    else:  # pragma: no cover — argparse already restricts choices
        sys.stderr.write(f"unknown backend: {args.backend!r}\n")
        return 2

    wav = tts.synthesize(args.test, voice=args.voice)
    args.out.write_bytes(wav)
    print(
        f"wrote {len(wav)} bytes ({len(args.test)} chars, backend={args.backend}) "
        f"→ {args.out}"
    )
    return 0


def main() -> None:
    # `python -m spindle_workers.audio_tts` (no args) — explain how to boot a worker.
    if len(sys.argv) == 1:
        sys.stderr.write(
            "audio_tts is not a worker entry point. To boot a worker:\n"
            "  python -m spindle_workers.audio_tts.openai\n"
            "  python -m spindle_workers.audio_tts.kokoro\n"
            "For one-shot synthesis:\n"
            "  python -m spindle_workers.audio_tts --test 'text' --backend openai\n"
        )
        sys.exit(2)
    args = _build_parser().parse_args()
    sys.exit(_run_test(args))


if __name__ == "__main__":
    main()
