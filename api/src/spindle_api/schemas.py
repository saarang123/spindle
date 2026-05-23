"""Request / response models for the API surface.

Most response models reuse the core domain types directly via
``model_dump(mode='json')`` — only request bodies and a few wrapped responses
need explicit schemas.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from spindle_core.types.artifact import ArtifactMeta
from spindle_core.types.errors import ErrorPayload
from spindle_core.types.job import JobStatus


# ─── jobs ────────────────────────────────────────────────────────────


class SubmitJobRequest(BaseModel):
    type: str
    config_id: str | None = None
    priority: int = 5
    idempotency_key: str | None = None
    requested_node: str | None = None
    timeout_seconds: int | None = None
    deadline_at: datetime | None = None
    workflow_id: UUID | None = None
    parent_job_ids: list[UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any]


class SubmitJobResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    created_at: datetime


# ─── worker lifecycle ───────────────────────────────────────────────


class StartJobRequest(BaseModel):
    lease_id: UUID
    worker_id: str
    attempt_id: UUID


class CompleteJobRequest(BaseModel):
    lease_id: UUID
    worker_id: str
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactMeta] = Field(default_factory=list)
    runtime: dict[str, Any] = Field(default_factory=dict)


class FailJobRequest(BaseModel):
    lease_id: UUID
    worker_id: str
    error: ErrorPayload
    runtime: dict[str, Any] = Field(default_factory=dict)


class OkResponse(BaseModel):
    ok: bool = True


# ─── model configs ──────────────────────────────────────────────────


class UpsertConfigRequest(BaseModel):
    id: str
    name: str
    version: str
    job_types: list[str]
    preferred_node: str | None = None
    runtime_backend: str
    model_ref: str
    params: dict[str, Any] = Field(default_factory=dict)
    resource_requirements: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class UpsertConfigResponse(BaseModel):
    ok: bool = True
    config_id: str


# ─── errors ─────────────────────────────────────────────────────────


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None


ErrorEnvelope.model_rebuild()
