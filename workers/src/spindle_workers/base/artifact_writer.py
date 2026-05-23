"""Per-job artifact writer.

Wraps an ``ArtifactStore`` so workers can ``ctx.artifacts.write(...)`` during
``execute()``. Tracks ``ArtifactMeta`` records in memory so they can be POSTed
to the API as part of ``/complete``.

v0: artifacts batch in memory and ship via ``/complete``. If a worker crashes
between ``put`` (bytes in MinIO) and ``/complete`` (metadata in Mongo), the
bytes are orphaned until a future janitor reconciles. Acceptable until job
durations grow long enough to warrant per-artifact API registration.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from spindle_core.artifacts.protocol import ArtifactStore
from spindle_core.types.artifact import ArtifactKind, ArtifactMeta

log = logging.getLogger("spindle_workers")


class ArtifactWriter:
    """Job-scoped helper. One per execute() invocation."""

    def __init__(self, store: ArtifactStore, job_id: UUID) -> None:
        self._store = store
        self._job_id = job_id
        self._artifacts: list[ArtifactMeta] = []

    @property
    def collected(self) -> list[ArtifactMeta]:
        """Returns all ArtifactMeta recorded so far. Caller passes this to
        ``ApiClient.complete(artifacts=...)``."""
        return list(self._artifacts)

    async def write(
        self,
        key: str,
        data: bytes | AsyncIterator[bytes],
        *,
        kind: ArtifactKind,
        mime_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        parent_artifact_ids: list[UUID] | None = None,
    ) -> ArtifactMeta:
        """Upload bytes via the store, build and remember the ArtifactMeta."""
        full_key = f"{self._job_id}/{key}"
        uri = await self._store.put(full_key, data, content_type=mime_type)

        size_bytes: int | None = None
        if isinstance(data, (bytes, bytearray)):
            size_bytes = len(data)

        meta = ArtifactMeta(
            id=uuid4(),
            job_id=self._job_id,
            kind=kind,
            uri=uri,
            mime_type=mime_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            metadata=metadata or {},
            parent_artifact_ids=parent_artifact_ids or [],
            created_at=datetime.now(UTC),
        )
        self._artifacts.append(meta)
        log.info(
            "artifact written: kind=%s key=%s uri=%s size=%s",
            kind.value, full_key, uri, size_bytes,
        )
        return meta
