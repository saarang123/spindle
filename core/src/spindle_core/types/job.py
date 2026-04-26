"""The Job domain model — the central record in the system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from spindle_core._time import utcnow
from spindle_core.types.errors import ErrorPayload


class JobStatus(StrEnum):
    """Lifecycle states. Transitions are constrained — see ARCHITECTURE.md §4."""

    CREATED = "created"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    DEAD_LETTERED = "dead_lettered"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.DEAD_LETTERED,
    }
)


class Job(BaseModel):
    """A single unit of work.

    Mongo storage:
      - `_id` in Mongo = `id` here (no separate ObjectId).
      - All UUIDs are persisted as native BSON UUIDs (subtype 4) via the motor
        client's uuidRepresentation="standard" setting.
      - Datetimes are tz-aware UTC; BSON Date is millisecond precision.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # identity
    id: UUID = Field(default_factory=uuid4)

    # classification
    type: str
    status: JobStatus = JobStatus.CREATED
    priority: int = Field(default=5, ge=0, le=10)

    # scheduling routing
    config_id: str | None = None
    requested_node: str | None = None

    # grouping / lineage
    workflow_id: UUID | None = None
    parent_job_ids: list[UUID] = Field(default_factory=list)

    # client-supplied
    idempotency_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # payloads
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: ErrorPayload | None = None

    # execution / lease
    assigned_worker_id: str | None = None
    lease_id: UUID | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False

    # retries / deadlines
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=2, ge=0)
    timeout_seconds: int | None = Field(default=None, ge=1)
    deadline_at: datetime | None = None

    # timestamps
    created_at: datetime = Field(default_factory=utcnow)
    queued_at: datetime | None = None
    leased_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
