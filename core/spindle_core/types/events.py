"""Append-only events emitted by the API, dispatcher, and workers.

Phase 1 stores events in the StateStore for durability. Phase 8 mirrors them
to ClickHouse for fast historical queries.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from spindle_core._time import utcnow


class JobEventType(StrEnum):
    SUBMITTED = "job.submitted"
    QUEUED = "job.queued"
    LEASED = "job.leased"
    STARTED = "job.started"
    PROGRESS = "job.progress"
    SUCCEEDED = "job.succeeded"
    FAILED = "job.failed"
    RETRYING = "job.retrying"
    CANCELED = "job.canceled"
    DEAD_LETTERED = "job.dead_lettered"


class JobEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    type: JobEventType
    job_id: UUID
    worker_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utcnow)
