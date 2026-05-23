"""In-process MemoryArtifactStore — tests and single-process dev.

URI scheme: "memory://<key>". State lives in a dict on the instance — not
shared across processes, not durable.
"""

from __future__ import annotations

from urllib.parse import urlparse

from spindle_core.artifacts.protocol import ArtifactStat


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._content_types: dict[str, str | None] = {}
        self._metadata: dict[str, dict[str, str]] = {}

    @staticmethod
    def _key_from_uri(uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != "memory":
            raise ValueError(f"not a memory:// URI: {uri!r}")
        # urlparse on "memory://abc/foo" → netloc="abc", path="/foo".
        # Re-stitch and strip the leading slash.
        return f"{parsed.netloc}{parsed.path}".lstrip("/")

    @staticmethod
    def _uri(key: str) -> str:
        return f"memory://{key}"

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        self._objects[key] = data
        self._content_types[key] = content_type
        self._metadata[key] = dict(metadata or {})
        return self._uri(key)

    async def get(self, uri: str) -> bytes:
        key = self._key_from_uri(uri)
        if key not in self._objects:
            raise FileNotFoundError(uri)
        return self._objects[key]

    async def stat(self, uri: str) -> ArtifactStat | None:
        key = self._key_from_uri(uri)
        if key not in self._objects:
            return None
        return ArtifactStat(
            uri=uri,
            size_bytes=len(self._objects[key]),
            content_type=self._content_types.get(key),
            etag=None,
        )

    async def delete(self, uri: str) -> None:
        key = self._key_from_uri(uri)
        self._objects.pop(key, None)
        self._content_types.pop(key, None)
        self._metadata.pop(key, None)

    async def signed_url(self, uri: str, *, ttl_seconds: int = 3600) -> str | None:
        # Memory backend can't sign — caller serves via API proxy.
        return None
