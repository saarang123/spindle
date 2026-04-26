"""StateStore protocol — the contract every state backend implements.

Backends are structurally typed; they don't inherit from this. The protocol is
runtime-checkable so we can `isinstance(store, StateStore)` at boundaries.

Scope (this iteration): job, event, and lease operations.
Workers, artifacts, and model_configs will be added as those features land —
extending the protocol is a non-breaking change for backends that don't yet
implement them (Pyright will flag missing methods at the call site).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from spindle_core.types.events import JobEvent
from spindle_core.types.job import Job, JobStatus


@runtime_checkable
class StateStore(Protocol):
    # ─── jobs ─────────────────────────────────────────────────────────

    async def create_job(self, job: Job) -> Job:
        """Insert a new job; for new jobs only.

        ID handling: `Job.id` is auto-generated (`uuid4`) if the caller didn't
        pass one. Either way, the returned `Job` carries the final id — read
        `result.id` after the call.

        Raises:
          - on duplicate `id` (caller bug — supplied an existing id)
          - `IdempotencyConflictError` on duplicate `idempotency_key` (caller should
            retry via `find_by_idempotency_key`).

        For status changes, use `transition`, `acquire_lease`, etc. — never
        `create_job` again on an existing id.
        """
        ...

    async def get_job(self, job_id: UUID) -> Job | None: ...

    async def find_by_idempotency_key(self, key: str) -> Job | None: ...

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        type: str | None = None,
        config_id: str | None = None,
        limit: int = 100,
    ) -> list[Job]: ...

    # ─── transitions (atomic single-document CAS) ────────────────────

    async def transition(
        self,
        job_id: UUID,
        *,
        expected_from: JobStatus | list[JobStatus],
        to: JobStatus,
        patch: dict[str, Any] | None = None,
    ) -> Job | None:
        """Atomic CAS transition. Returns the updated Job, or None if the
        precondition (`status in expected_from`) failed.

        Implementations MUST update `updated_at` to now() automatically.
        Implementations MAY auto-set timestamp fields based on `to`:
            QUEUED → set queued_at if null
            LEASED → set leased_at if null
            RUNNING → set started_at if null
            SUCCEEDED|FAILED|CANCELED|DEAD_LETTERED → set completed_at if null
        """
        ...

    async def acquire_lease(
        self,
        job_id: UUID,
        worker_id: str,
        lease_id: UUID,
        expires_at: datetime,
    ) -> Job | None:
        """Atomic: transition queued → leased and stamp lease fields.
        Returns updated Job, or None if status was not 'queued'.
        """
        ...

    async def extend_lease(
        self,
        job_id: UUID,
        lease_id: UUID,
        new_expires_at: datetime,
    ) -> bool:
        """Update lease_expires_at iff lease_id matches and job is leased/running.
        Returns False on mismatch.
        """
        ...

    async def request_cancel(self, job_id: UUID) -> bool:
        """Idempotent set of cancel_requested=True. Returns True if the job
        existed (regardless of whether the flag was already set), False if not.
        """
        ...

    # ─── sweepers ────────────────────────────────────────────────────

    async def find_expired_leases(self, now: datetime, limit: int = 50) -> list[Job]:
        """Jobs with status in {leased, running} whose lease_expires_at < now."""
        ...

    async def find_overdue_jobs(self, now: datetime, limit: int = 50) -> list[Job]:
        """Jobs with deadline_at < now in non-terminal state."""
        ...

    # ─── events ──────────────────────────────────────────────────────

    async def record_event(self, event: JobEvent) -> None: ...

    async def list_events(
        self,
        job_id: UUID,
        *,
        after: datetime | None = None,
        limit: int = 200,
    ) -> list[JobEvent]: ...


class IdempotencyConflictError(Exception):
    """Raised by create_job when idempotency_key already exists."""

    def __init__(self, key: str, existing_job_id: UUID | None = None) -> None:
        super().__init__(f"idempotency_key already exists: {key!r}")
        self.key = key
        self.existing_job_id = existing_job_id
