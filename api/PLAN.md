# `api/` — FastAPI gateway

The only HTTP-facing service. Clients submit jobs here; workers report lifecycle here. The dispatcher does NOT go through here — it talks to `StateStore` and `JobQueue` directly.

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) sections 3 (Components → api) and 4 (Data flow). Depends on [`../core/PLAN.md`](../core/PLAN.md) (Phase 1 must be done).

## Goal

A FastAPI app that exposes the endpoint surface below, with idempotent submission, structured logging, and a health endpoint. Backed by `StateStore` + `JobQueue` from `core`. No scheduler logic, no worker dispatch logic.

## Package

`spindle_api`. Layout:

```
api/
  pyproject.toml
  src/spindle_api/
    __init__.py
    app.py                      # FastAPI app factory + lifespan (start/stop StateStore/JobQueue)
    deps.py                     # FastAPI dependency-injection: settings, store, queue, auth
    routes/
      __init__.py
      health.py
      jobs.py
      workers.py
      artifacts.py
    schemas/
      __init__.py
      jobs.py                   # request/response models (NOT the core Job; api-shaped)
      workers.py
      artifacts.py
      common.py                 # ErrorResponse, etc.
    services/
      submit.py                 # idempotency check + create_job + enqueue
      lifecycle.py              # start/progress/complete/fail handlers
      events.py                 # event emission helper
    middleware/
      auth.py                   # bearer-token check (optional)
      request_id.py
      logging.py
    main.py                     # `uvicorn spindle_api.main:app` entrypoint
  tests/
    test_jobs_api.py
    test_workers_api.py
    test_artifacts_api.py
    test_idempotency.py
    test_lifecycle.py
    conftest.py                 # in-memory store + queue fixtures, async test client
```

## Endpoints

All requests/responses are JSON. UUIDs are stringified. Datetimes are ISO-8601 with `Z`.

### Health

```
GET /health → 200
{ "status": "ok", "node": "control", "version": "0.1.0" }
```

### Submit job

```
POST /jobs
Body:
{
  "type": "text.generate",
  "config_id": "qwen-text-v1",                     # optional; required for some types
  "priority": 5,                                   # optional, default 5
  "idempotency_key": "client-supplied-key",        # optional
  "requested_node": "control",                     # optional override
  "timeout_seconds": 120,                          # optional
  "deadline_at": "2026-04-26T10:00:00Z",           # optional
  "workflow_id": "...",                            # optional (for grouping)
  "parent_job_ids": ["..."],                       # optional (for lineage)
  "tags": ["content"],
  "metadata": { ... },
  "input": { "prompt": "...", "max_tokens": 1024 } # required, validated by job-type registry
}

→ 201
{
  "job_id": "uuid",
  "status": "queued",
  "created_at": "..."
}

If idempotency_key matches an existing job → 200 with the existing job_id (not 201).
```

Submission flow (in `services/submit.py`):
1. Resolve `type` against the **job type registry** (a tiny dict in `core` listing valid types and required input fields). Reject `UNSUPPORTED_JOB_TYPE`.
2. If `config_id` is provided, `state.get_config(config_id)` — reject `MODEL_CONFIG_NOT_FOUND` if missing or inactive.
3. If `idempotency_key` provided, `state.find_by_idempotency_key(key)`. If hit, return existing.
4. Build `Job` with `status=queued`, `created_at`/`queued_at=now`.
5. `state.create_job(job)`. On unique-violation on idempotency_key (race), retry the find.
6. `queue.enqueue(config_id_or_default, job_id, priority=priority)`.
7. `state.record_event(JobEvent(type=submitted))` then `record_event(QUEUED)`.
8. Return.

If step 6 fails after step 5, the job stays in `queued` state but is missing from the queue. The dispatcher's startup recovery sweep handles this — log a warning, return 201 anyway.

### Get job

```
GET /jobs/{job_id} → 200
{
  "id": "...",
  "type": "...",
  "status": "...",
  "priority": 5,
  "config_id": "...",
  "assigned_worker_id": "...",
  "input": {...},
  "output": {...},
  "error": {...},
  "artifacts": [{...}],          # populated via list_artifacts_for_job
  "retry_count": 0,
  "created_at": "...",
  "queued_at": "...",
  "leased_at": "...",
  "started_at": "...",
  "completed_at": "...",
  "metadata": {...},
  "tags": [...]
}

404 if not found.
```

### Cancel job

```
POST /jobs/{job_id}/cancel → 200
Body: {} (or { "reason": "..." })
{ "job_id": "...", "cancel_requested": true, "current_status": "running" }
```

Sets `cancel_requested=true` on the job. Worker polls and aborts. If job is `queued` (no worker yet), transition directly to `canceled`.

### List jobs (admin / CLI)

```
GET /jobs?status=queued&type=text.generate&config_id=...&limit=50 → 200
{ "jobs": [ {...}, ... ] }
```

### Worker lifecycle endpoints

These are called by workers, NOT the dispatcher.

#### Start (worker → API: I'm now running this job)

```
POST /jobs/{job_id}/start
Body:
{
  "lease_id": "uuid",
  "worker_id": "control-text-0",
  "attempt_id": "uuid"          # client-generated, stored on the job for telemetry
}
→ 200
{ "ok": true }
```

API verifies `lease_id` matches and transitions `leased → running`. Records `started_at`.

#### Progress

```
POST /jobs/{job_id}/progress
Body:
{
  "lease_id": "uuid",
  "worker_id": "...",
  "phase": "sampling",          # optional free-form
  "step": 17,
  "total_steps": 40,
  "percent": 42.5,
  "message": "..."              # optional
}
→ 200 { "ok": true, "cancel_requested": false }
```

Returns the cancel flag so the worker can abort without a separate poll. (This is the cheap path; `cancel_poll` endpoint below is for workers that don't emit progress.)

#### Cancel poll

```
GET /jobs/{job_id}/cancel_status
→ 200 { "cancel_requested": true }
```

For workers in long GPU loops with no natural progress checkpoint.

#### Extend lease

```
POST /jobs/{job_id}/extend_lease
Body:
{
  "lease_id": "uuid",
  "worker_id": "...",
  "extend_seconds": 120
}
→ 200 { "ok": true, "lease_expires_at": "..." }
→ 409 if lease_id mismatch or job no longer running
```

#### Complete

```
POST /jobs/{job_id}/complete
Body:
{
  "lease_id": "uuid",
  "worker_id": "...",
  "output": { ... },
  "artifacts": [
    {
      "kind": "text",
      "uri": "file:///mnt/artifacts/...",
      "mime_type": "text/plain",
      "size_bytes": 1234,
      "metadata": { ... }
    }
  ],
  "runtime": {
    "execution_ms": 923,
    "tokens_in": 120,
    "tokens_out": 300
  }
}
→ 200 { "ok": true }
```

API: verify lease, transition `running → succeeded`, set `output` + `completed_at`, record artifacts via `state.record_artifact`, emit `job.succeeded` event.

#### Fail

```
POST /jobs/{job_id}/fail
Body:
{
  "lease_id": "uuid",
  "worker_id": "...",
  "error": {
    "code": "MODEL_RUNTIME_ERROR",
    "message": "...",
    "retryable": true,
    "details": { ... }
  },
  "runtime": { "execution_ms": 2000 }
}
→ 200 { "ok": true, "will_retry": true }
```

API logic:
- Verify lease.
- If `retryable && retry_count < max_retries`: increment `retry_count`, transition `running → queued` (back to queue), re-enqueue, schedule with backoff (set `queued_at` to `now + backoff`).
  - For Phase 2 we do immediate re-enqueue; backoff scheduling lands in dispatcher.
- Else: transition `running → failed` (or `dead_lettered` if retries exhausted).
- Emit appropriate event.

### Worker registration / heartbeat

```
POST /workers/{worker_id}/heartbeat
Body:
{
  "node": "control",
  "status": "busy",
  "capabilities": ["text.generate"],
  "config_id": "qwen-text-v1",
  "loaded_model": "qwen-a3b",
  "runtime_backend": "mlx",
  "concurrency_used": 2,
  "concurrency_limit": 4,
  "current_job_ids": ["..."],
  "ipc_socket": "/tmp/spindle-worker-text-0.sock",
  "resource": { "memory_used_gb": 31.2, ... },
  "last_error": null
}
→ 200 { "ok": true, "server_time": "..." }
```

`upsert_worker` with `last_heartbeat_at=now`. First call from a new `worker_id` registers; subsequent calls update.

### List workers

```
GET /workers → 200
{
  "workers": [ { ...Worker... }, ... ]
}
```

Optional query params: `?node=control&fresher_than_seconds=30`.

### Artifacts

```
GET /artifacts/{artifact_id} → 200 ArtifactMeta
GET /artifacts?job_id={job_id} → 200 { "artifacts": [...] }
GET /artifacts/{artifact_id}/bytes → 200 stream
```

`/bytes` resolves the URI via the local `ArtifactStore` and streams it. If backend is `local` and the file is on this node, serve from disk. If backend is `http` (control node fetching from GPU), proxy the request.

## Auth

Optional bearer token via `SPINDLE_API_AUTH_TOKEN`. If unset, all routes are open (LAN-only deployment). If set, every request must carry `Authorization: Bearer <token>`. `/health` is always open.

## Error format

All errors use:

```json
{
  "error": {
    "code": "MODEL_CONFIG_NOT_FOUND",
    "message": "config 'qwen-text-v9' does not exist",
    "request_id": "uuid"
  }
}
```

Mapped from `ErrorCode` enum where possible.

## Logging

structlog. Bind `request_id`, `job_id`, `worker_id` into context. Output JSON or text per `SPINDLE_LOG_FORMAT`.

## Acceptance criteria

- [ ] `uv run pytest api/` passes against `MemoryStateStore` + `MemoryQueue`.
- [ ] `uv run uvicorn spindle_api.main:app` boots against the docker compose infra without error.
- [ ] All endpoints listed above respond with correct status codes (verified by tests).
- [ ] Idempotency: submitting the same `idempotency_key` twice returns the same `job_id` and does NOT enqueue twice (verified).
- [ ] Lease guards: completing/failing with wrong `lease_id` returns 409.
- [ ] Cancel of a `queued` job moves to `canceled` directly; cancel of a `running` job sets the flag.
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Workflow/DAG endpoints (deferred).
- History query / shard / eval endpoints (Phase 7).
- ClickHouse event mirroring (Phase 8).
- Multi-tenant scoping.
- Rate limiting / quota enforcement.
- `POST /workers/{id}/lease` — leasing is dispatcher-side only; no API endpoint.
