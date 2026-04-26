"""MongoDB-backed StateStore.

Storage shape:
  - Database: configurable via SPINDLE_MONGO_DB (default "spindle").
  - Collection `jobs`: one doc per Job; `_id` is the Job's UUID.
  - Collection `job_events`: append-only events; `_id` is the event UUID.

Atomic operations use single-document CAS via `find_one_and_update`. Status
transitions use pipeline updates so we can `$ifNull` timestamp fields
(preserving first-time-entered semantics for queued_at/leased_at/started_at).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from spindle_core._time import utcnow
from spindle_core.state._serialization import from_doc, to_doc
from spindle_core.state.protocol import IdempotencyConflictError
from spindle_core.types.events import JobEvent
from spindle_core.types.job import Job, JobStatus

# Maps target status → which timestamp field to set (first-time only).
_STATUS_TIMESTAMP: dict[JobStatus, str] = {
    JobStatus.QUEUED: "queued_at",
    JobStatus.LEASED: "leased_at",
    JobStatus.RUNNING: "started_at",
    JobStatus.SUCCEEDED: "completed_at",
    JobStatus.FAILED: "completed_at",
    JobStatus.CANCELED: "completed_at",
    JobStatus.DEAD_LETTERED: "completed_at",
}


def _normalize_expected(expected: JobStatus | list[JobStatus]) -> list[str]:
    if isinstance(expected, JobStatus):
        return [expected.value]
    return [s.value for s in expected]


class MongoStateStore:
    """Mongo-backed implementation of the StateStore protocol.

    Construct directly with a connection URL, or via the `make_state_store`
    factory in `spindle_core.state`.
    """

    def __init__(
        self,
        url: str,
        db_name: str = "spindle",
        *,
        client: AsyncIOMotorClient | None = None,
        validate_on_read: bool = True,
    ) -> None:
        # `tz_aware=True` makes motor return UTC-aware datetimes on read instead
        # of naive — required because we use tz-aware datetimes everywhere and
        # naive/aware comparisons raise.
        # `uuidRepresentation="standard"` encodes Python UUIDs as BSON Binary
        # subtype 4 (the modern, language-agnostic representation).
        self._client = client or AsyncIOMotorClient(
            url, uuidRepresentation="standard", tz_aware=True
        )
        self._db: AsyncIOMotorDatabase = self._client[db_name]
        self.jobs: AsyncIOMotorCollection = self._db["jobs"]
        self.events: AsyncIOMotorCollection = self._db["job_events"]
        self._validate_on_read = validate_on_read

    # ─── lifecycle ───────────────────────────────────────────────────

    async def ensure_indexes(self) -> None:
        """Idempotently create indexes. Call once at startup."""
        # Partial index: only enforce uniqueness when idempotency_key is a string.
        # Sparse indexes don't help here because Pydantic emits the field as
        # explicit None on docs without an idempotency key — Mongo sees the
        # field as "present" and would reject duplicate nulls.
        await self.jobs.create_index(
            [("idempotency_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
            name="idempotency_key_unique",
        )
        await self.jobs.create_index([("status", ASCENDING)], name="status")
        await self.jobs.create_index(
            [("status", ASCENDING), ("type", ASCENDING), ("priority", DESCENDING)],
            name="status_type_priority",
        )
        await self.jobs.create_index([("lease_expires_at", ASCENDING)], name="lease_expires_at")
        await self.jobs.create_index([("deadline_at", ASCENDING)], name="deadline_at")
        await self.jobs.create_index(
            [("config_id", ASCENDING), ("status", ASCENDING)],
            name="config_id_status",
        )
        await self.events.create_index(
            [("job_id", ASCENDING), ("occurred_at", ASCENDING)],
            name="job_id_occurred_at",
        )

    def close(self) -> None:
        self._client.close()

    # ─── jobs ────────────────────────────────────────────────────────

    async def create_job(self, job: Job) -> Job:
        doc = to_doc(job)
        try:
            await self.jobs.insert_one(doc)
        except DuplicateKeyError as e:
            # Could be _id collision (caller bug) or idempotency_key collision.
            if job.idempotency_key is not None and "idempotency_key" in str(e):
                raise IdempotencyConflictError(job.idempotency_key) from e
            raise
        return job

    async def get_job(self, job_id: UUID) -> Job | None:
        doc = await self.jobs.find_one({"_id": job_id})
        return from_doc(Job, doc, validate=self._validate_on_read) if doc else None

    async def find_by_idempotency_key(self, key: str) -> Job | None:
        doc = await self.jobs.find_one({"idempotency_key": key})
        return from_doc(Job, doc, validate=self._validate_on_read) if doc else None

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        type: str | None = None,
        config_id: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        filter_: dict[str, Any] = {}
        if status is not None:
            filter_["status"] = status.value
        if type is not None:
            filter_["type"] = type
        if config_id is not None:
            filter_["config_id"] = config_id
        cursor = self.jobs.find(filter_).limit(limit)
        return [from_doc(Job, d, validate=self._validate_on_read) async for d in cursor]

    # ─── transitions ─────────────────────────────────────────────────

    async def transition(
        self,
        job_id: UUID,
        *,
        expected_from: JobStatus | list[JobStatus],
        to: JobStatus,
        patch: dict[str, Any] | None = None,
    ) -> Job | None:
        now = utcnow()
        set_fields: dict[str, Any] = {
            "status": to.value,
            "updated_at": now,
        }
        if patch:
            set_fields.update(patch)

        # First-time-only timestamp via $ifNull (pipeline-form $set).
        ts_field = _STATUS_TIMESTAMP.get(to)
        if ts_field:
            set_fields[ts_field] = {"$ifNull": [f"${ts_field}", now]}

        doc = await self.jobs.find_one_and_update(
            {"_id": job_id, "status": {"$in": _normalize_expected(expected_from)}},
            [{"$set": set_fields}],
            return_document=ReturnDocument.AFTER,
        )
        return from_doc(Job, doc, validate=self._validate_on_read) if doc else None

    async def acquire_lease(
        self,
        job_id: UUID,
        worker_id: str,
        lease_id: UUID,
        expires_at: datetime,
    ) -> Job | None:
        now = utcnow()
        doc = await self.jobs.find_one_and_update(
            {"_id": job_id, "status": JobStatus.QUEUED.value},
            [
                {
                    "$set": {
                        "status": JobStatus.LEASED.value,
                        "assigned_worker_id": worker_id,
                        "lease_id": lease_id,
                        "lease_expires_at": expires_at,
                        "leased_at": {"$ifNull": ["$leased_at", now]},
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        return from_doc(Job, doc, validate=self._validate_on_read) if doc else None

    async def extend_lease(
        self,
        job_id: UUID,
        lease_id: UUID,
        new_expires_at: datetime,
    ) -> bool:
        now = utcnow()
        result = await self.jobs.update_one(
            {
                "_id": job_id,
                "lease_id": lease_id,
                "status": {"$in": [JobStatus.LEASED.value, JobStatus.RUNNING.value]},
            },
            {"$set": {"lease_expires_at": new_expires_at, "updated_at": now}},
        )
        return result.modified_count == 1

    async def request_cancel(self, job_id: UUID) -> bool:
        now = utcnow()
        result = await self.jobs.update_one(
            {"_id": job_id},
            {"$set": {"cancel_requested": True, "updated_at": now}},
        )
        return result.matched_count == 1

    # ─── sweepers ────────────────────────────────────────────────────

    async def find_expired_leases(self, now: datetime, limit: int = 50) -> list[Job]:
        cursor = self.jobs.find(
            {
                "status": {"$in": [JobStatus.LEASED.value, JobStatus.RUNNING.value]},
                "lease_expires_at": {"$lt": now},
            }
        ).limit(limit)
        return [from_doc(Job, d, validate=self._validate_on_read) async for d in cursor]

    async def find_overdue_jobs(self, now: datetime, limit: int = 50) -> list[Job]:
        terminal = [
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELED.value,
            JobStatus.DEAD_LETTERED.value,
        ]
        cursor = self.jobs.find(
            {
                "deadline_at": {"$lt": now, "$ne": None},
                "status": {"$nin": terminal},
            }
        ).limit(limit)
        return [from_doc(Job, d, validate=self._validate_on_read) async for d in cursor]

    # ─── events ──────────────────────────────────────────────────────

    async def record_event(self, event: JobEvent) -> None:
        await self.events.insert_one(to_doc(event))

    async def list_events(
        self,
        job_id: UUID,
        *,
        after: datetime | None = None,
        limit: int = 200,
    ) -> list[JobEvent]:
        filter_: dict[str, Any] = {"job_id": job_id}
        if after is not None:
            filter_["occurred_at"] = {"$gt": after}
        cursor = self.events.find(filter_).sort("occurred_at", ASCENDING).limit(limit)
        return [from_doc(JobEvent, d, validate=self._validate_on_read) async for d in cursor]

    # ─── test/admin helpers ──────────────────────────────────────────

    async def _drop_all(self) -> None:
        """Test-only. Drops jobs + events collections."""
        await self.jobs.drop()
        await self.events.drop()


__all__ = ["MongoStateStore"]
