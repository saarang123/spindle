"""FastAPI app factory + lifespan + uvicorn entry.

Endpoints (v0 minimum):

    GET    /health
    POST   /jobs                  — submit (idempotent on idempotency_key)
    GET    /jobs/{job_id}         — poll; includes recorded artifacts
    POST   /jobs/{job_id}/start   — worker lifecycle: leased → running
    POST   /jobs/{job_id}/complete — worker lifecycle: running → succeeded
    POST   /jobs/{job_id}/fail    — worker lifecycle: running → failed
    POST   /configs               — upsert ModelConfig (admin / seed)
    GET    /configs/{config_id}   — fetch one
    GET    /artifacts/{artifact_id}/bytes — stream artifact body via ArtifactStore

Deferred (per dispatcher merge plan):
    POST /workers/{id}/heartbeat        — runtime owns process liveness
    POST /jobs/{id}/extend_lease        — use long initial leases instead
    POST /jobs/{id}/cancel              — add when cancel UX matters
    GET  /jobs/{id}/cancel_status       — same
    Retry-on-fail with backoff scheduling — keep failures terminal in v0
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

# Pull SPINDLE_* and any sibling secrets out of .env into os.environ before
# Settings is instantiated.
load_dotenv()

from spindle_core.artifacts import make_artifact_store
from spindle_core.artifacts.protocol import ArtifactStore
from spindle_core.queue import make_queue
from spindle_core.queue.protocol import JobQueue
from spindle_core.settings import Settings
from spindle_core.state import IdempotencyConflictError, make_state_store
from spindle_core.state.protocol import StateStore
from spindle_core.types.config import ModelConfig
from spindle_core.types.events import JobEvent, JobEventType
from spindle_core.types.job import Job, JobStatus

from .schemas import (
    CompleteJobRequest,
    FailJobRequest,
    OkResponse,
    StartJobRequest,
    SubmitJobRequest,
    SubmitJobResponse,
    UpsertConfigRequest,
    UpsertConfigResponse,
)

log = logging.getLogger("spindle_api")


# ─── lifespan + factory ─────────────────────────────────────────────


@asynccontextmanager
async def _default_lifespan(app: FastAPI):
    settings = Settings()
    app.state.spindle_settings = settings
    app.state.spindle_state = make_state_store(settings)
    app.state.spindle_queue = make_queue(settings)
    app.state.spindle_artifacts = make_artifact_store(settings)
    log.info(
        "spindle-api lifespan started (state=%s, queue=%s, artifacts=%s)",
        settings.state_backend,
        settings.queue_backend,
        settings.artifact_backend,
    )
    yield
    log.info("spindle-api lifespan stopped")


def create_app(
    *,
    state: StateStore | None = None,
    queue: JobQueue | None = None,
    artifacts: ArtifactStore | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    If any of ``state`` / ``queue`` / ``artifacts`` are supplied, the lifespan
    backend bootstrap is skipped (tests inject backends directly). Otherwise
    backends are created from ``Settings`` on startup.
    """
    if state or queue or artifacts:
        app = FastAPI(title="Spindle API", version=__import__("spindle_api").__version__)
        app.state.spindle_state = state
        app.state.spindle_queue = queue
        app.state.spindle_artifacts = artifacts
    else:
        app = FastAPI(
            title="Spindle API",
            version=__import__("spindle_api").__version__,
            lifespan=_default_lifespan,
        )

    _register_routes(app)
    return app


# ─── routes ─────────────────────────────────────────────────────────


def _state(request: Request) -> StateStore:
    return request.app.state.spindle_state


def _queue(request: Request) -> JobQueue:
    return request.app.state.spindle_queue


def _artifacts(request: Request) -> ArtifactStore:
    return request.app.state.spindle_artifacts


def _now() -> datetime:
    return datetime.now(UTC)


def _register_routes(app: FastAPI) -> None:
    # ─── health ──────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "spindle-api"}

    # ─── jobs: submit / get ──────────────────────────────────────────

    @app.post("/jobs", response_model=SubmitJobResponse, status_code=201)
    async def submit_job(req: SubmitJobRequest, request: Request) -> SubmitJobResponse:
        state = _state(request)
        queue = _queue(request)

        # Idempotency: hit-existing path.
        if req.idempotency_key:
            existing = await state.find_by_idempotency_key(req.idempotency_key)
            if existing is not None:
                return SubmitJobResponse(
                    job_id=existing.id,
                    status=existing.status,
                    created_at=existing.created_at,
                )

        # Resolve / validate config_id when supplied.
        if req.config_id:
            cfg = await state.get_config(req.config_id)
            if cfg is None or not cfg.is_active:
                raise HTTPException(
                    400,
                    detail=f"config_id {req.config_id!r} not found or inactive",
                )

        now = _now()
        job = Job(
            id=uuid4(),
            type=req.type,
            status=JobStatus.QUEUED,
            priority=req.priority,
            config_id=req.config_id,
            requested_node=req.requested_node,
            workflow_id=req.workflow_id,
            parent_job_ids=req.parent_job_ids,
            idempotency_key=req.idempotency_key,
            tags=req.tags,
            metadata=req.metadata,
            input=req.input,
            timeout_seconds=req.timeout_seconds,
            deadline_at=req.deadline_at,
            created_at=now,
            queued_at=now,
            updated_at=now,
        )

        try:
            saved = await state.create_job(job)
        except IdempotencyConflictError:
            # Race: another caller created with the same key.
            assert req.idempotency_key is not None
            existing = await state.find_by_idempotency_key(req.idempotency_key)
            if existing is None:
                raise
            return SubmitJobResponse(
                job_id=existing.id,
                status=existing.status,
                created_at=existing.created_at,
            )

        # Enqueue (if config_id is provided — Spindle's queue is per-config).
        if saved.config_id:
            await queue.ensure_group(saved.config_id)
            await queue.enqueue(saved.config_id, saved.id, priority=saved.priority)

        # Record the submitted event.
        await state.record_event(
            JobEvent(
                id=uuid4(),
                type=JobEventType.SUBMITTED,
                job_id=saved.id,
                payload={},
                occurred_at=now,
            )
        )

        return SubmitJobResponse(
            job_id=saved.id,
            status=saved.status,
            created_at=saved.created_at,
        )

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: UUID, request: Request) -> dict:
        state = _state(request)
        job = await state.get_job(job_id)
        if job is None:
            raise HTTPException(404, detail="job not found")
        artifacts = await state.list_artifacts_for_job(job.id)
        return {
            **job.model_dump(mode="json"),
            "artifacts": [a.model_dump(mode="json") for a in artifacts],
        }

    # ─── jobs: worker lifecycle ──────────────────────────────────────

    @app.post("/jobs/{job_id}/start", response_model=OkResponse)
    async def start_job(
        job_id: UUID,
        req: StartJobRequest,
        request: Request,
    ) -> OkResponse:
        state = _state(request)
        job = await state.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.lease_id != req.lease_id:
            raise HTTPException(409, "lease_id mismatch")

        updated = await state.transition(
            job_id,
            expected_from=JobStatus.LEASED,
            to=JobStatus.RUNNING,
        )
        if updated is None:
            raise HTTPException(409, "cannot transition to running")

        await state.record_event(
            JobEvent(
                id=uuid4(),
                type=JobEventType.STARTED,
                job_id=job_id,
                worker_id=req.worker_id,
                payload={"attempt_id": str(req.attempt_id)},
                occurred_at=_now(),
            )
        )
        return OkResponse()

    @app.post("/jobs/{job_id}/complete", response_model=OkResponse)
    async def complete_job(
        job_id: UUID,
        req: CompleteJobRequest,
        request: Request,
    ) -> OkResponse:
        state = _state(request)
        job = await state.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.lease_id != req.lease_id:
            raise HTTPException(409, "lease_id mismatch")

        # Record artifacts first so they're durable even if the transition
        # races with another writer.
        for art in req.artifacts:
            await state.record_artifact(art)

        updated = await state.transition(
            job_id,
            expected_from=JobStatus.RUNNING,
            to=JobStatus.SUCCEEDED,
            patch={"output": req.output},
        )
        if updated is None:
            raise HTTPException(409, "cannot transition to succeeded")

        await state.record_event(
            JobEvent(
                id=uuid4(),
                type=JobEventType.SUCCEEDED,
                job_id=job_id,
                worker_id=req.worker_id,
                payload={"runtime": req.runtime},
                occurred_at=_now(),
            )
        )
        return OkResponse()

    @app.post("/jobs/{job_id}/fail", response_model=OkResponse)
    async def fail_job(
        job_id: UUID,
        req: FailJobRequest,
        request: Request,
    ) -> OkResponse:
        state = _state(request)
        job = await state.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.lease_id != req.lease_id:
            raise HTTPException(409, "lease_id mismatch")

        # v0: no retry / dead-letter logic. Failure is terminal.
        updated = await state.transition(
            job_id,
            expected_from=JobStatus.RUNNING,
            to=JobStatus.FAILED,
            patch={"error": req.error.model_dump()},
        )
        if updated is None:
            raise HTTPException(409, "cannot transition to failed")

        await state.record_event(
            JobEvent(
                id=uuid4(),
                type=JobEventType.FAILED,
                job_id=job_id,
                worker_id=req.worker_id,
                payload={"runtime": req.runtime, "error_code": req.error.code.value},
                occurred_at=_now(),
            )
        )
        return OkResponse()

    # ─── model configs ───────────────────────────────────────────────

    @app.post("/configs", response_model=UpsertConfigResponse)
    async def upsert_config(
        req: UpsertConfigRequest,
        request: Request,
    ) -> UpsertConfigResponse:
        state = _state(request)
        config = ModelConfig(
            id=req.id,
            name=req.name,
            version=req.version,
            job_types=req.job_types,
            preferred_node=req.preferred_node,
            runtime_backend=req.runtime_backend,
            model_ref=req.model_ref,
            params=req.params,
            resource_requirements=req.resource_requirements,
            is_active=req.is_active,
            created_at=_now(),
        )
        await state.upsert_config(config)
        return UpsertConfigResponse(config_id=config.id)

    @app.get("/configs/{config_id}")
    async def get_config(config_id: str, request: Request) -> dict:
        cfg = await _state(request).get_config(config_id)
        if cfg is None:
            raise HTTPException(404, "config not found")
        return cfg.model_dump(mode="json")

    # ─── artifacts ───────────────────────────────────────────────────

    @app.get("/artifacts/{artifact_id}/bytes")
    async def get_artifact_bytes(
        artifact_id: UUID,
        request: Request,
    ) -> StreamingResponse:
        state = _state(request)
        artifacts = _artifacts(request)
        meta = await state.get_artifact(artifact_id)
        if meta is None:
            raise HTTPException(404, "artifact not found")
        return StreamingResponse(
            artifacts.get(meta.uri),
            media_type=meta.mime_type or "application/octet-stream",
        )


# ─── module-level app + uvicorn entry ──────────────────────────────


# Define after _register_routes so the closure captures it; uvicorn picks
# this up as `spindle_api.main:app`.
app = create_app()


def run() -> None:
    """`spindle-api` script entry point."""
    settings = Settings()
    uvicorn.run(
        "spindle_api.main:app",
        host="0.0.0.0",
        port=8080,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
