"""MongoStateStore behavior tests against an in-process mongomock backend.

For real-mongo fidelity, mark equivalent tests with @pytest.mark.integration
once we wire up testcontainers — same assertions, different fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from spindle_core import (
    ErrorCode,
    ErrorPayload,
    Job,
    JobEvent,
    JobEventType,
    JobStatus,
)
from spindle_core.state.mongo import MongoStateStore
from spindle_core.state.protocol import IdempotencyConflictError, StateStore


def _make_job(**overrides) -> Job:  # type: ignore[no-untyped-def]
    defaults = {"type": "cpu.echo", "input": {"message": "hi"}}
    defaults.update(overrides)
    return Job(**defaults)


# ─── conformance — protocol satisfied ────────────────────────────────


def test_mongo_state_store_satisfies_protocol(state_store: MongoStateStore) -> None:
    assert isinstance(state_store, StateStore)


# ─── jobs ────────────────────────────────────────────────────────────


async def test_create_and_get_job(state_store: MongoStateStore) -> None:
    job = _make_job()
    await state_store.create_job(job)

    fetched = await state_store.get_job(job.id)
    assert fetched is not None
    assert fetched.id == job.id
    assert fetched.type == "cpu.echo"
    assert fetched.input == {"message": "hi"}


async def test_create_job_auto_generates_id(state_store: MongoStateStore) -> None:
    """Caller doesn't pass an id; the Job's default_factory generates one and
    it round-trips through create_job and get_job."""
    job = Job(type="cpu.echo", input={})
    assert job.id is not None
    returned = await state_store.create_job(job)
    assert returned.id == job.id

    fetched = await state_store.get_job(returned.id)
    assert fetched is not None
    assert fetched.id == returned.id


async def test_validate_on_read_off_returns_raw(
    state_store: MongoStateStore,
) -> None:
    """With validation off, status comes back as a raw string, not the enum."""
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    state_store._validate_on_read = False
    fetched = await state_store.get_job(job.id)
    assert fetched is not None
    # model_construct skips coercion: enum stays as the underlying str
    assert fetched.status == "queued"
    # but does NOT equal the enum member by identity
    assert fetched.status is not JobStatus.QUEUED


async def test_get_missing_returns_none(state_store: MongoStateStore) -> None:
    assert await state_store.get_job(uuid4()) is None


async def test_idempotency_key_collision_raises(state_store: MongoStateStore) -> None:
    job1 = _make_job(idempotency_key="dup")
    await state_store.create_job(job1)
    with pytest.raises(IdempotencyConflictError):
        await state_store.create_job(_make_job(idempotency_key="dup"))


async def test_find_by_idempotency_key(state_store: MongoStateStore) -> None:
    job = _make_job(idempotency_key="abc-123")
    await state_store.create_job(job)
    found = await state_store.find_by_idempotency_key("abc-123")
    assert found is not None
    assert found.id == job.id
    assert await state_store.find_by_idempotency_key("missing") is None


async def test_list_jobs_filters(state_store: MongoStateStore) -> None:
    a = _make_job(type="text.generate", status=JobStatus.QUEUED)
    b = _make_job(type="text.generate", status=JobStatus.SUCCEEDED)
    c = _make_job(type="image.generate", status=JobStatus.QUEUED)
    for j in (a, b, c):
        await state_store.create_job(j)

    queued = await state_store.list_jobs(status=JobStatus.QUEUED)
    assert {j.id for j in queued} == {a.id, c.id}

    text_only = await state_store.list_jobs(type="text.generate")
    assert {j.id for j in text_only} == {a.id, b.id}

    queued_text = await state_store.list_jobs(status=JobStatus.QUEUED, type="text.generate")
    assert [j.id for j in queued_text] == [a.id]


# ─── transitions ─────────────────────────────────────────────────────


async def test_transition_succeeds_when_precondition_matches(
    state_store: MongoStateStore,
) -> None:
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    updated = await state_store.transition(
        job.id, expected_from=JobStatus.QUEUED, to=JobStatus.LEASED
    )
    assert updated is not None
    assert updated.status == JobStatus.LEASED
    assert updated.leased_at is not None


async def test_transition_rejects_wrong_precondition(
    state_store: MongoStateStore,
) -> None:
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    result = await state_store.transition(
        job.id, expected_from=JobStatus.RUNNING, to=JobStatus.SUCCEEDED
    )
    assert result is None

    fetched = await state_store.get_job(job.id)
    assert fetched is not None
    assert fetched.status == JobStatus.QUEUED


async def test_transition_accepts_status_list(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.LEASED)
    await state_store.create_job(job)

    updated = await state_store.transition(
        job.id,
        expected_from=[JobStatus.LEASED, JobStatus.RUNNING],
        to=JobStatus.SUCCEEDED,
    )
    assert updated is not None
    assert updated.status == JobStatus.SUCCEEDED
    assert updated.completed_at is not None


async def test_transition_preserves_first_timestamp(
    state_store: MongoStateStore,
) -> None:
    """A second QUEUED transition (after a retry) must not overwrite queued_at."""
    job = _make_job(status=JobStatus.CREATED)
    await state_store.create_job(job)

    first = await state_store.transition(
        job.id, expected_from=JobStatus.CREATED, to=JobStatus.QUEUED
    )
    assert first is not None
    first_queued_at = first.queued_at
    assert first_queued_at is not None

    # Simulate retry path: leased -> queued again
    await state_store.transition(job.id, expected_from=JobStatus.QUEUED, to=JobStatus.LEASED)
    second = await state_store.transition(
        job.id, expected_from=JobStatus.LEASED, to=JobStatus.QUEUED
    )
    assert second is not None
    assert second.queued_at == first_queued_at


async def test_transition_applies_patch(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.RUNNING)
    await state_store.create_job(job)

    err = ErrorPayload(code=ErrorCode.MODEL_RUNTIME_ERROR, message="boom", retryable=True)
    updated = await state_store.transition(
        job.id,
        expected_from=JobStatus.RUNNING,
        to=JobStatus.FAILED,
        patch={"error": err.model_dump(mode="python"), "retry_count": 1},
    )
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    assert updated.error == err
    assert updated.retry_count == 1


# ─── leases ──────────────────────────────────────────────────────────


async def test_acquire_lease_success(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    lease_id = uuid4()
    expires = datetime.now(UTC) + timedelta(seconds=60)
    leased = await state_store.acquire_lease(
        job.id, worker_id="w-0", lease_id=lease_id, expires_at=expires
    )
    assert leased is not None
    assert leased.status == JobStatus.LEASED
    assert leased.lease_id == lease_id
    assert leased.assigned_worker_id == "w-0"
    assert leased.lease_expires_at is not None


async def test_acquire_lease_fails_when_not_queued(
    state_store: MongoStateStore,
) -> None:
    job = _make_job(status=JobStatus.RUNNING)
    await state_store.create_job(job)

    result = await state_store.acquire_lease(
        job.id, "w-0", uuid4(), datetime.now(UTC) + timedelta(seconds=60)
    )
    assert result is None


async def test_acquire_lease_is_atomic(state_store: MongoStateStore) -> None:
    """Two concurrent acquires on the same queued job — only one wins."""
    import asyncio

    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    expires = datetime.now(UTC) + timedelta(seconds=60)
    a, b = await asyncio.gather(
        state_store.acquire_lease(job.id, "w-a", uuid4(), expires),
        state_store.acquire_lease(job.id, "w-b", uuid4(), expires),
    )
    winners = [r for r in (a, b) if r is not None]
    assert len(winners) == 1


async def test_extend_lease_success(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)

    lease_id = uuid4()
    # BSON Date is millisecond precision; truncate microseconds so equality
    # holds after the round trip.
    initial_expires = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
    leased = await state_store.acquire_lease(job.id, "w-0", lease_id, initial_expires)
    assert leased is not None

    new_expires = initial_expires + timedelta(seconds=60)
    ok = await state_store.extend_lease(job.id, lease_id, new_expires)
    assert ok is True

    refreshed = await state_store.get_job(job.id)
    assert refreshed is not None
    assert refreshed.lease_expires_at == new_expires


async def test_extend_lease_rejects_wrong_id(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.QUEUED)
    await state_store.create_job(job)
    await state_store.acquire_lease(
        job.id, "w-0", uuid4(), datetime.now(UTC) + timedelta(seconds=60)
    )

    ok = await state_store.extend_lease(job.id, uuid4(), datetime.now(UTC) + timedelta(seconds=120))
    assert ok is False


# ─── cancellation ────────────────────────────────────────────────────


async def test_request_cancel_sets_flag(state_store: MongoStateStore) -> None:
    job = _make_job(status=JobStatus.RUNNING)
    await state_store.create_job(job)

    assert await state_store.request_cancel(job.id) is True
    refreshed = await state_store.get_job(job.id)
    assert refreshed is not None
    assert refreshed.cancel_requested is True


async def test_request_cancel_missing_returns_false(
    state_store: MongoStateStore,
) -> None:
    assert await state_store.request_cancel(uuid4()) is False


# ─── sweepers ────────────────────────────────────────────────────────


async def test_find_expired_leases(state_store: MongoStateStore) -> None:
    now = datetime.now(UTC)
    fresh = _make_job(
        status=JobStatus.LEASED,
        lease_expires_at=now + timedelta(seconds=60),
    )
    expired = _make_job(
        status=JobStatus.RUNNING,
        lease_expires_at=now - timedelta(seconds=10),
    )
    not_leased = _make_job(status=JobStatus.QUEUED)
    for j in (fresh, expired, not_leased):
        await state_store.create_job(j)

    found = await state_store.find_expired_leases(now)
    assert {j.id for j in found} == {expired.id}


async def test_find_overdue_jobs(state_store: MongoStateStore) -> None:
    now = datetime.now(UTC)
    overdue = _make_job(
        status=JobStatus.RUNNING,
        deadline_at=now - timedelta(seconds=5),
    )
    fresh = _make_job(
        status=JobStatus.RUNNING,
        deadline_at=now + timedelta(seconds=60),
    )
    terminal_overdue = _make_job(
        status=JobStatus.SUCCEEDED,
        deadline_at=now - timedelta(seconds=5),
    )
    no_deadline = _make_job(status=JobStatus.RUNNING)
    for j in (overdue, fresh, terminal_overdue, no_deadline):
        await state_store.create_job(j)

    found = await state_store.find_overdue_jobs(now)
    assert {j.id for j in found} == {overdue.id}


# ─── events ──────────────────────────────────────────────────────────


async def test_record_and_list_events(state_store: MongoStateStore) -> None:
    job = _make_job()
    await state_store.create_job(job)

    e1 = JobEvent(type=JobEventType.SUBMITTED, job_id=job.id)
    e2 = JobEvent(type=JobEventType.QUEUED, job_id=job.id)
    e3 = JobEvent(type=JobEventType.LEASED, job_id=job.id, worker_id="w-0")
    for e in (e1, e2, e3):
        await state_store.record_event(e)

    events = await state_store.list_events(job.id)
    assert [e.type for e in events] == [
        JobEventType.SUBMITTED,
        JobEventType.QUEUED,
        JobEventType.LEASED,
    ]
    assert events[2].worker_id == "w-0"


async def test_list_events_filters_by_after(state_store: MongoStateStore) -> None:
    job = _make_job()
    await state_store.create_job(job)

    cutoff = datetime.now(UTC)
    later = JobEvent(
        type=JobEventType.SUCCEEDED,
        job_id=job.id,
        occurred_at=cutoff + timedelta(seconds=1),
    )
    earlier = JobEvent(
        type=JobEventType.SUBMITTED,
        job_id=job.id,
        occurred_at=cutoff - timedelta(seconds=1),
    )
    await state_store.record_event(later)
    await state_store.record_event(earlier)

    after = await state_store.list_events(job.id, after=cutoff)
    assert [e.type for e in after] == [JobEventType.SUCCEEDED]
