"""ArtifactStore protocol — the third swap point.

Stores raw bytes addressed by an opaque URI. Backends include `s3` (MinIO,
AWS S3, Cloudflare R2 — same wire protocol) and `memory` (tests / dev).

URIs are backend-specific strings:
    s3       → "s3://bucket-name/key/path.png"
    memory   → "memory://key/path.png"

Callers never parse them. They round-trip put → get/stat/delete unchanged.

Semantics:
  - put() returns the canonical URI for the stored object.
  - get() returns the full body as bytes. Use signed_url for big downloads
    that should bypass the API. Streaming variant can land later if needed.
  - stat() returns None if the URI doesn't exist.
  - delete() is idempotent — deleting a missing URI doesn't raise.
  - signed_url() returns None for backends that can't generate one
    (e.g., memory). Callers fall back to api-proxy serving.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ArtifactStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    size_bytes: int
    content_type: str | None = None
    etag: str | None = None


@runtime_checkable
class ArtifactStore(Protocol):
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Store bytes under `key`. Returns canonical URI.

        `key` is a logical path like "abc-123/output.png". Backend translates.
        `metadata` is opaque k/v stored alongside (S3 user metadata, etc.).
        Existing key is overwritten.
        """
        ...

    async def get(self, uri: str) -> bytes:
        """Return the full object body. Raises FileNotFoundError if missing."""
        ...

    async def stat(self, uri: str) -> ArtifactStat | None:
        """Return size + content type without downloading. None if missing."""
        ...

    async def delete(self, uri: str) -> None:
        """Idempotent: missing URIs do not raise."""
        ...

    async def signed_url(self, uri: str, *, ttl_seconds: int = 3600) -> str | None:
        """Pre-signed URL for direct client download. None if unsupported."""
        ...
