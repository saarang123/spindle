"""ArtifactStore conformance suite.

Parametrized over [memory, s3]. Memory always runs; S3 runs against a real
MinIO if SPINDLE_S3_SECRET_KEY is set in the environment (loaded from .env
by conftest if present).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import pytest

from spindle_core.artifacts.protocol import ArtifactStore


def _key_for(store: ArtifactStore, suffix: str) -> str:
    """Scope keys per-store-instance using the test prefix the fixture sets
    (S3) or just the suffix (memory)."""
    prefix = getattr(store, "_test_prefix", "")
    return f"{prefix}{suffix}"


# ─── conformance — protocol satisfied ────────────────────────────────


def test_satisfies_protocol(artifact_store: ArtifactStore) -> None:
    assert isinstance(artifact_store, ArtifactStore)


# ─── put + get round trip ────────────────────────────────────────────


async def test_put_and_get_small_bytes(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "hello.txt")
    payload = b"hello world"
    uri = await artifact_store.put(key, payload, content_type="text/plain")

    got = await artifact_store.get(uri)
    assert got == payload


async def test_put_and_get_binary(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "blob.bin")
    payload = bytes(range(256)) * 16  # 4 KB, all byte values represented
    uri = await artifact_store.put(key, payload)

    got = await artifact_store.get(uri)
    assert got == payload
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest()


async def test_put_overwrites_existing(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "overwrite.txt")
    uri1 = await artifact_store.put(key, b"first")
    uri2 = await artifact_store.put(key, b"second")
    assert uri1 == uri2  # same key → same URI

    got = await artifact_store.get(uri1)
    assert got == b"second"


# ─── stat ────────────────────────────────────────────────────────────


async def test_stat_returns_size_and_mime(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "stat.png")
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
    uri = await artifact_store.put(key, payload, content_type="image/png")

    stat = await artifact_store.stat(uri)
    assert stat is not None
    assert stat.uri == uri
    assert stat.size_bytes == len(payload)
    assert stat.content_type == "image/png"


async def test_stat_missing_returns_none(artifact_store: ArtifactStore) -> None:
    # Construct a URI of the same scheme as the store, but for a missing key.
    sample_uri = await artifact_store.put(_key_for(artifact_store, "alive.txt"), b"x")
    parsed = urlparse(sample_uri)
    missing_uri = f"{parsed.scheme}://{parsed.netloc}/_test_/missing-{__name__}.bin"

    assert await artifact_store.stat(missing_uri) is None


# ─── get on missing raises ───────────────────────────────────────────


async def test_get_missing_raises_file_not_found(
    artifact_store: ArtifactStore,
) -> None:
    sample_uri = await artifact_store.put(_key_for(artifact_store, "alive2.txt"), b"x")
    parsed = urlparse(sample_uri)
    missing_uri = f"{parsed.scheme}://{parsed.netloc}/_test_/no-such-key-{__name__}.bin"

    with pytest.raises(FileNotFoundError):
        await artifact_store.get(missing_uri)


# ─── delete ──────────────────────────────────────────────────────────


async def test_delete_removes_object(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "to-delete.txt")
    uri = await artifact_store.put(key, b"goodbye")
    assert (await artifact_store.stat(uri)) is not None

    await artifact_store.delete(uri)
    assert (await artifact_store.stat(uri)) is None


async def test_delete_missing_is_idempotent(artifact_store: ArtifactStore) -> None:
    sample_uri = await artifact_store.put(_key_for(artifact_store, "alive3.txt"), b"x")
    parsed = urlparse(sample_uri)
    missing_uri = f"{parsed.scheme}://{parsed.netloc}/_test_/never-existed.bin"

    # Should not raise.
    await artifact_store.delete(missing_uri)


# ─── signed_url ──────────────────────────────────────────────────────


async def test_signed_url_returns_url_or_none(artifact_store: ArtifactStore) -> None:
    key = _key_for(artifact_store, "for-signing.txt")
    uri = await artifact_store.put(key, b"signed-content")

    url = await artifact_store.signed_url(uri, ttl_seconds=60)
    # Memory backend returns None; S3 returns a string starting with http(s).
    assert url is None or url.startswith("http")


# ─── realistic-sized payload ─────────────────────────────────────────


async def test_put_and_get_real_image(artifact_store: ArtifactStore) -> None:
    """Round-trips a 555KB JPEG. Confirms multi-chunk upload/download works."""
    fixture = Path(__file__).parent / "fixtures" / "example_500kb.jpg"
    if not fixture.exists():  # pragma: no cover
        pytest.skip(f"fixture missing: {fixture}")

    payload = fixture.read_bytes()
    assert len(payload) > 100_000

    key = _key_for(artifact_store, "example_500kb.jpg")
    uri = await artifact_store.put(key, payload, content_type="image/jpeg")

    got = await artifact_store.get(uri)
    assert len(got) == len(payload)
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(payload).hexdigest()

    stat = await artifact_store.stat(uri)
    assert stat is not None
    assert stat.size_bytes == len(payload)
    assert stat.content_type == "image/jpeg"
