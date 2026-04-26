"""Pure model tests — no Mongo, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from spindle_core import (
    RETRYABLE_ERROR_CODES,
    TERMINAL_STATUSES,
    ErrorCode,
    ErrorPayload,
    Job,
    JobEvent,
    JobEventType,
    JobStatus,
    Lease,
)


def test_job_defaults() -> None:
    job = Job(type="cpu.echo", input={"message": "hi"}, config_id="cpu-echo-v1")
    assert job.id is not None
    assert job.status == JobStatus.CREATED
    assert job.priority == 5
    assert job.retry_count == 0
    assert job.max_retries == 2
    assert job.cancel_requested is False
    assert job.parent_job_ids == []
    assert job.tags == []
    assert job.metadata == {}
    assert job.created_at.tzinfo is UTC
    assert job.updated_at.tzinfo is UTC
    assert job.is_terminal is False


def test_job_requires_config_id() -> None:
    """config_id is mandatory (per-config queue routing requires it)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Job(type="cpu.echo", input={})  # type: ignore[call-arg]


def test_job_round_trip_dict() -> None:
    job = Job(
        type="text.generate",
        config_id="qwen-text-v1",
        priority=7,
        input={"prompt": "hi", "max_tokens": 100},
        tags=["content"],
    )
    data = job.model_dump(mode="python")
    rebuilt = Job.model_validate(data)
    assert rebuilt == job


def test_job_status_terminal_membership() -> None:
    assert JobStatus.SUCCEEDED in TERMINAL_STATUSES
    assert JobStatus.FAILED in TERMINAL_STATUSES
    assert JobStatus.CANCELED in TERMINAL_STATUSES
    assert JobStatus.DEAD_LETTERED in TERMINAL_STATUSES
    assert JobStatus.QUEUED not in TERMINAL_STATUSES
    assert JobStatus.RUNNING not in TERMINAL_STATUSES


def test_job_is_terminal_property() -> None:
    job = Job(type="cpu.echo", input={}, config_id="cpu-echo-v1", status=JobStatus.SUCCEEDED)
    assert job.is_terminal is True


def test_job_priority_bounds() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Job(type="cpu.echo", input={}, config_id="cpu-echo-v1", priority=11)
    with pytest.raises(ValidationError):
        Job(type="cpu.echo", input={}, config_id="cpu-echo-v1", priority=-1)


def test_error_payload_round_trip() -> None:
    err = ErrorPayload(
        code=ErrorCode.MODEL_RUNTIME_ERROR,
        message="oom",
        retryable=True,
        details={"gpu_mem": 0},
    )
    data = err.model_dump(mode="python")
    assert data["code"] == "MODEL_RUNTIME_ERROR"
    rebuilt = ErrorPayload.model_validate(data)
    assert rebuilt == err


def test_retryable_error_codes_are_retryable() -> None:
    assert ErrorCode.MODEL_RUNTIME_ERROR in RETRYABLE_ERROR_CODES
    assert ErrorCode.INVALID_INPUT not in RETRYABLE_ERROR_CODES
    assert ErrorCode.DEADLINE_EXCEEDED not in RETRYABLE_ERROR_CODES


def test_job_with_error_payload() -> None:
    job = Job(
        type="cpu.echo",
        input={},
        config_id="cpu-echo-v1",
        status=JobStatus.FAILED,
        error=ErrorPayload(
            code=ErrorCode.WORKER_LOST,
            message="lease expired",
            retryable=False,
        ),
    )
    data = job.model_dump(mode="python")
    rebuilt = Job.model_validate(data)
    assert rebuilt.error == job.error


def test_job_event_defaults() -> None:
    event = JobEvent(type=JobEventType.QUEUED, job_id=uuid4())
    assert event.id is not None
    assert event.payload == {}
    assert event.occurred_at.tzinfo is UTC


def test_lease_round_trip() -> None:
    lease = Lease(
        id=uuid4(),
        job_id=uuid4(),
        worker_id="control-text-0",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    data = lease.model_dump(mode="python")
    rebuilt = Lease.model_validate(data)
    assert rebuilt == lease
