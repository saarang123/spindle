"""Error codes and payloads. Used by Job.error and surfaced to callers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    # client / input errors — never retryable
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_JOB_TYPE = "UNSUPPORTED_JOB_TYPE"
    MODEL_CONFIG_NOT_FOUND = "MODEL_CONFIG_NOT_FOUND"
    AUTH_FAILED = "AUTH_FAILED"
    SAFETY_REJECTED = "SAFETY_REJECTED"

    # transient — retryable by default
    MODEL_RUNTIME_ERROR = "MODEL_RUNTIME_ERROR"
    WORKER_LOST = "WORKER_LOST"
    TRANSIENT_NETWORK_ERROR = "TRANSIENT_NETWORK_ERROR"
    EXTERNAL_API_TIMEOUT = "EXTERNAL_API_TIMEOUT"
    ARTIFACT_UPLOAD_FAILED = "ARTIFACT_UPLOAD_FAILED"

    # terminal regardless of retry budget
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.MODEL_RUNTIME_ERROR,
        ErrorCode.WORKER_LOST,
        ErrorCode.TRANSIENT_NETWORK_ERROR,
        ErrorCode.EXTERNAL_API_TIMEOUT,
        ErrorCode.ARTIFACT_UPLOAD_FAILED,
    }
)


class ErrorPayload(BaseModel):
    """Structured error attached to a failed job.

    `retryable` is the worker's classification at fail-time. The dispatcher's
    retry policy may still override (e.g., retry budget exhausted).
    """

    code: ErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)
