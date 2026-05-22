"""Internal helpers shared across TTS backends."""
from __future__ import annotations

import io
import re
import wave


def chunk_text(text: str, limit: int) -> list[str]:
    """Sentence-aware text chunker.

    Each returned chunk is at most ``limit`` characters. Splits happen at
    sentence boundaries (``.``, ``!``, ``?``) when possible; a single sentence
    longer than the limit falls back to a hard split.
    """
    if len(text) <= limit:
        return [text]

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(sent) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(sent), limit):
                chunks.append(sent[i : i + limit])
            continue
        candidate = f"{current} {sent}" if current else sent
        if len(candidate) > limit:
            chunks.append(current)
            current = sent
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def concat_wav(wav_blobs: list[bytes]) -> bytes:
    """Concatenate WAV byte blobs into one WAV.

    Assumes every input has the same channel count, sample width, and frame
    rate (true across our backends; all produce 24 kHz mono).
    """
    if not wav_blobs:
        return b""
    if len(wav_blobs) == 1:
        return wav_blobs[0]

    out = io.BytesIO()
    first = wave.open(io.BytesIO(wav_blobs[0]), "rb")
    try:
        with wave.open(out, "wb") as writer:
            writer.setnchannels(first.getnchannels())
            writer.setsampwidth(first.getsampwidth())
            writer.setframerate(first.getframerate())
            writer.writeframes(first.readframes(first.getnframes()))
            for blob in wav_blobs[1:]:
                reader = wave.open(io.BytesIO(blob), "rb")
                try:
                    writer.writeframes(reader.readframes(reader.getnframes()))
                finally:
                    reader.close()
    finally:
        first.close()
    return out.getvalue()


def samples_to_wav(samples, sample_rate: int) -> bytes:
    """Encode a 1-D float32 sample array in [-1, 1] as mono 16-bit WAV bytes.

    ``samples`` is anything numpy-array-like (numpy is imported lazily so this
    module stays importable without it).
    """
    import numpy as np

    int_samples = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(int_samples.tobytes())
    return out.getvalue()


def wav_duration_seconds(wav_bytes: bytes) -> float:
    """Inspect a WAV blob and return its duration in seconds."""
    if not wav_bytes:
        return 0.0
    reader = wave.open(io.BytesIO(wav_bytes), "rb")
    try:
        frames = reader.getnframes()
        rate = reader.getframerate()
        if rate == 0:
            return 0.0
        return frames / rate
    finally:
        reader.close()
