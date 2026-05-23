"""Artifact metadata — what the StateStore records about each file produced
or consumed by jobs. The bytes themselves live in an ArtifactStore (S3/MinIO).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from spindle_core._time import utcnow


class ArtifactKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    JSON = "json"
    LOG = "log"
    THUMBNAIL = "thumbnail"
    BINARY = "binary"


class ArtifactMeta(BaseModel):
    """Pointer + metadata for a stored artifact.

    Mongo storage:
      - Collection `artifacts`. `_id` = `id` (UUID).
      - Indexed by `(job_id)` for the per-job listing query.

    `uri` is opaque — backends own the format ("s3://...", "memory://...").
    Callers pass it to ArtifactStore.get/stat/delete; never parse it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(default_factory=uuid4)
    job_id: UUID | None = None  # None for client-uploaded inputs not yet bound to a job
    workflow_id: UUID | None = None

    kind: ArtifactKind
    uri: str
    mime_type: str | None = None
    size_bytes: int | None = None

    # optional dimensions
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None

    hash_sha256: str | None = None
    parent_artifact_ids: list[UUID] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
