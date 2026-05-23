# `core/` — domain types, protocols, backends

This is the foundation. Every other component depends on `core/` and nothing else from this repo. **Land this before anyone else starts.**

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) sections 3, 4, 5 first.

## Goal

1. Define the **domain model** (Pydantic) — `Job`, `Worker`, `ArtifactMeta`, `Lease`, `ModelConfig`, `JobEvent`.
2. Define the three **swap-point protocols** — `StateStore`, `JobQueue`, `ArtifactStore`.
3. Implement v0 backends — `mongo` state, `redis` queue, `local` + `http` artifacts. Plus `memory` impl per protocol for tests.
4. Define `Settings` (pydantic-settings) consuming env vars from `.env.example`.
5. Provide **factories** that select backends by env-var token.
6. Ship a **conformance test suite** that runs against every backend.

No HTTP server. No scheduler logic. No model-runtime imports.

## Package

`spindle_core` (importable as `from spindle_core import ...`).

Layout:

```
core/
  pyproject.toml
  src/spindle_core/
    __init__.py                 # re-exports the public API
    settings.py                 # Settings class
    types/
      __init__.py
      job.py                    # Job, JobStatus enum
      worker.py                 # Worker, WorkerStatus enum
      artifact.py               # ArtifactMeta, ArtifactKind
      lease.py                  # Lease
      config.py                 # ModelConfig
      events.py                 # JobEvent + event_type literals
      errors.py                 # ErrorCode enum, ErrorPayload
    state/
      __init__.py               # make_state_store factory
      protocol.py               # StateStore protocol
      mongo.py                  # MongoStateStore
      memory.py                 # MemoryStateStore
    queue/
      __init__.py               # make_queue factory
      protocol.py               # JobQueue protocol
      redis_streams.py          # RedisStreamsQueue
      memory.py                 # MemoryQueue
    artifacts/
      __init__.py               # make_artifact_store factory
      protocol.py               # ArtifactStore protocol
      local_fs.py               # LocalFsArtifactStore
      http_fetch.py             # HttpFetchArtifactStore (read-only fetcher)
      memory.py                 # MemoryArtifactStore
    logging.py                  # structlog setup
    ids.py                      # uuid helpers
  tests/
    conftest.py                 # fixtures: mongo, redis, tmp_path
    conformance/
      test_state_store.py       # parametrized over all backends
      test_job_queue.py
      test_artifact_store.py
    test_settings.py
```

## Domain types

All Pydantic v2. UUIDs are `uuid.UUID`. Timestamps are `datetime` with `tzinfo=UTC`.

### `Job`

```python
class JobStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    DEAD_LETTERED = "dead_lettered"

class Job(BaseModel):
    id: UUID
    type: str                                # e.g. "text.generate"
    status: JobStatus
    priority: int = 5                        # 0..10, higher = more urgent

    config_id: str | None = None             # ModelConfig.id
    requested_node: str | None = None
    workflow_id: UUID | None = None
    parent_job_ids: list[UUID] = []

    idempotency_key: str | None = None
    tags: list[str] = []
    metadata: dict[str, Any] = {}

    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: ErrorPayload | None = None

    assigned_worker_id: str | None = None
    lease_id: UUID | None = None
    lease_expires_at: datetime | None = None

    retry_count: int = 0
    max_retries: int = 2
    timeout_seconds: int | None = None
    deadline_at: datetime | None = None

    created_at: datetime
    queued_at: datetime | None = None
    leased_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
    cancel_requested: bool = False
```

### `Worker`

```python
class WorkerStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"

class Worker(BaseModel):
    id: str                                  # e.g. "control-text-0"
    node: str
    status: WorkerStatus
    capabilities: list[str]                  # job type prefixes worker accepts
    config_id: str | None                    # one config per worker process
    runtime_backend: str | None              # "mlx", "diffusers", "comfyui", "ffmpeg", ...
    loaded_model: str | None
    concurrency_used: int = 0
    concurrency_limit: int = 1
    current_job_ids: list[UUID] = []
    ipc_socket: str | None = None            # path the dispatcher writes to
    resource: dict[str, Any] = {}            # memory_used_gb, gpu_memory_used_gb, etc.
    last_error: ErrorPayload | None = None
    last_heartbeat_at: datetime
    created_at: datetime
    updated_at: datetime
```

### `ArtifactMeta`

```python
class ArtifactKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    JSON = "json"
    LOG = "log"
    THUMBNAIL = "thumbnail"
    BINARY = "binary"

class ArtifactMeta(BaseModel):
    id: UUID
    job_id: UUID | None
    workflow_id: UUID | None = None

    kind: ArtifactKind
    uri: str                                 # opaque; backend-specific scheme
    mime_type: str | None = None
    size_bytes: int | None = None

    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None

    hash_sha256: str | None = None
    parent_artifact_ids: list[UUID] = []     # lineage
    metadata: dict[str, Any] = {}

    created_at: datetime
```

### `ModelConfig`

```python
class ModelConfig(BaseModel):
    id: str                                  # e.g. "qwen-text-v1"
    name: str
    version: str
    job_types: list[str]                     # capabilities this config handles
    preferred_node: str | None = None
    runtime_backend: str
    model_ref: str
    params: dict[str, Any] = {}
    resource_requirements: dict[str, Any] = {}
    is_active: bool = True
    created_at: datetime
```

### `Lease`

Returned by `acquire_lease`; not stored as a separate document — fields live on `Job`.

```python
class Lease(BaseModel):
    id: UUID
    job_id: UUID
    worker_id: str
    expires_at: datetime
```

### `JobEvent`

Append-only; written by API / runtime / workers. Phase 1 stores them in `StateStore.record_event` for durability; Phase 8 mirrors to ClickHouse.

```python
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
    id: UUID
    type: JobEventType
    job_id: UUID
    worker_id: str | None = None
    payload: dict[str, Any] = {}
    occurred_at: datetime
```

### `ErrorPayload`

```python
class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_JOB_TYPE = "UNSUPPORTED_JOB_TYPE"
    MODEL_CONFIG_NOT_FOUND = "MODEL_CONFIG_NOT_FOUND"
    MODEL_RUNTIME_ERROR = "MODEL_RUNTIME_ERROR"
    WORKER_LOST = "WORKER_LOST"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TRANSIENT_NETWORK_ERROR = "TRANSIENT_NETWORK_ERROR"
    EXTERNAL_API_TIMEOUT = "EXTERNAL_API_TIMEOUT"
    ARTIFACT_UPLOAD_FAILED = "ARTIFACT_UPLOAD_FAILED"
    SAFETY_REJECTED = "SAFETY_REJECTED"
    AUTH_FAILED = "AUTH_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

RETRYABLE_ERROR_CODES: set[ErrorCode] = {
    ErrorCode.MODEL_RUNTIME_ERROR,
    ErrorCode.WORKER_LOST,
    ErrorCode.TRANSIENT_NETWORK_ERROR,
    ErrorCode.EXTERNAL_API_TIMEOUT,
    ErrorCode.ARTIFACT_UPLOAD_FAILED,
}

class ErrorPayload(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] = {}
```

## Protocols

All protocols are `typing.Protocol` (runtime-checkable). All methods are `async`.

### `StateStore` (`state/protocol.py`)

```python
class StateStore(Protocol):
    # jobs
    async def create_job(self, job: Job) -> Job: ...
    async def get_job(self, job_id: UUID) -> Job | None: ...
    async def find_by_idempotency_key(self, key: str) -> Job | None: ...
    async def list_jobs(
        self, *, status: JobStatus | None = None, type: str | None = None,
        config_id: str | None = None, limit: int = 100,
    ) -> list[Job]: ...

    # transitions — all atomic; return updated Job or None if precondition failed
    async def transition(
        self, job_id: UUID, *, expected_from: JobStatus | list[JobStatus],
        to: JobStatus, patch: dict[str, Any] | None = None,
    ) -> Job | None: ...

    async def acquire_lease(
        self, job_id: UUID, worker_id: str, lease_id: UUID, expires_at: datetime,
    ) -> Job | None: ...

    async def extend_lease(
        self, job_id: UUID, lease_id: UUID, new_expires_at: datetime,
    ) -> bool: ...

    async def request_cancel(self, job_id: UUID) -> bool: ...

    # sweepers
    async def find_expired_leases(self, now: datetime, limit: int = 50) -> list[Job]: ...
    async def find_overdue_jobs(self, now: datetime, limit: int = 50) -> list[Job]: ...

    # workers
    async def upsert_worker(self, worker: Worker) -> None: ...
    async def get_worker(self, worker_id: str) -> Worker | None: ...
    async def list_workers(
        self, *, fresher_than: datetime | None = None, node: str | None = None,
    ) -> list[Worker]: ...

    # configs (read-mostly; writes happen via config-loader bootstrap)
    async def upsert_config(self, config: ModelConfig) -> None: ...
    async def get_config(self, config_id: str) -> ModelConfig | None: ...
    async def list_configs(self, *, active_only: bool = True) -> list[ModelConfig]: ...

    # artifacts
    async def record_artifact(self, art: ArtifactMeta) -> ArtifactMeta: ...
    async def get_artifact(self, artifact_id: UUID) -> ArtifactMeta | None: ...
    async def list_artifacts_for_job(self, job_id: UUID) -> list[ArtifactMeta]: ...

    # events
    async def record_event(self, event: JobEvent) -> None: ...
    async def list_events(
        self, job_id: UUID, *, after: datetime | None = None, limit: int = 200,
    ) -> list[JobEvent]: ...
```

**Atomicity requirements:**
- `transition`, `acquire_lease`, `extend_lease`, `request_cancel` MUST be atomic single-document CAS. No "read then write".
- `acquire_lease` returns `None` if `status != 'queued'` at the moment of update.
- `extend_lease` returns `False` if `lease_id` doesn't match.

### `JobQueue` (`queue/protocol.py`)

```python
class Reserved(BaseModel):
    job_id: UUID
    config_id: str
    reservation_id: str                      # opaque; pass to ack/nack
    delivery_count: int = 1                  # 1 on first delivery, increments on reclaim

class JobQueue(Protocol):
    async def enqueue(
        self, config_id: str, job_id: UUID, *, priority: int = 5,
    ) -> None: ...

    async def reserve(
        self, config_ids: list[str], consumer: str,
        *, count: int = 1, block_ms: int = 1000,
    ) -> list[Reserved]: ...

    async def ack(self, config_id: str, reservation_id: str) -> None: ...
    async def nack(
        self, config_id: str, reservation_id: str, *, requeue: bool = True,
    ) -> None: ...

    async def depth(self, config_id: str) -> int: ...

    async def reclaim_stale(
        self, config_id: str, consumer: str, *, idle_ms: int,
    ) -> list[Reserved]: ...

    async def ensure_group(self, config_id: str) -> None:
        """Idempotent. Creates the consumer group / stream if missing."""
```

**Semantics:**
- At-least-once delivery. Workers + dispatcher must be idempotent.
- `reserve` blocks for up to `block_ms`. Returns `[]` on timeout.
- `nack(requeue=True)` makes the message available again immediately. `requeue=False` drops it (ack-equivalent without success).
- Priority is best-effort; Redis Streams impl uses two streams per config (`:hi`, `:lo`) and prefers hi.

### `ArtifactStore` (`artifacts/protocol.py`)

```python
class ArtifactStat(BaseModel):
    uri: str
    size_bytes: int
    content_type: str | None
    etag: str | None = None

class ArtifactStore(Protocol):
    async def put(
        self, key: str, data: bytes | AsyncIterator[bytes],
        *, content_type: str | None = None, metadata: dict[str, str] | None = None,
    ) -> str:
        """Returns canonical URI."""

    async def get(self, uri: str) -> AsyncIterator[bytes]: ...
    async def stat(self, uri: str) -> ArtifactStat | None: ...
    async def delete(self, uri: str) -> None: ...
    async def signed_url(self, uri: str, *, ttl_seconds: int = 3600) -> str | None:
        """Returns None if backend can't sign (caller should serve via API proxy)."""
```

**Key shape:** `key` is a logical path like `{job_id}/output.png`. Backend converts to URI:
- `local`: `file:///mnt/artifacts/{yyyy}/{mm}/{dd}/{job_id}/output.png`
- `http`: `http://gpu.local:8090/artifacts/{job_id}/output.png` (read-only; writes happen via local FS path on the worker side)
- `memory`: `memory://{key}`

`HttpFetchArtifactStore` is **read-only** — its `put` raises. It's used on the control node when the worker side has `LocalFsArtifactStore`. Two stores cooperate: the worker writes locally, the API proxies reads via HTTP from the worker host.

## Settings

`spindle_core/settings.py`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPINDLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    node: str = "control"

    # api (read by api/, but lives here so workers can find the URL)
    api_url: str = "http://localhost:8080"
    api_bind: str = "0.0.0.0:8080"
    api_auth_token: SecretStr | None = None

    # backends
    state_backend: Literal["mongo", "memory"] = "mongo"
    queue_backend: Literal["redis", "memory"] = "redis"
    artifact_backend: Literal["local", "http"] = "local"

    # mongo
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "spindle"

    # redis
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_prefix: str = "spindle:queue"
    redis_consumer_group: str = "dispatchers"

    # artifacts
    artifact_root: Path = Path("/mnt/artifacts")
    artifact_uri_scheme: str = "file"
    artifact_http_base: str = "http://localhost:8090"

    # logging
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # … see .env.example for the full list; keep this in sync.
```

## Factories

```python
# state/__init__.py
def make_state_store(settings: Settings) -> StateStore:
    match settings.state_backend:
        case "mongo":
            from .mongo import MongoStateStore
            return MongoStateStore(settings)
        case "memory":
            from .memory import MemoryStateStore
            return MemoryStateStore()
        case other:
            raise ConfigError(f"unknown state backend: {other!r}")
```

Same shape for `make_queue` and `make_artifact_store`. **Lazy imports inside match arms** so missing optional drivers don't blow up at import time.

## Backend impl notes

### `MongoStateStore`

- Single collection `jobs`. Indexes: `(idempotency_key)` unique sparse, `(status)`, `(status, type, priority)`, `(lease_expires_at)`, `(deadline_at)`, `(config_id, status)`.
- Collection `workers` keyed by `id`. Indexes: `(node)`, `(last_heartbeat_at)`.
- Collection `model_configs` keyed by `id`.
- Collection `artifacts` keyed by `id`. Indexes: `(job_id)`.
- Collection `job_events` capped at 10M documents (or use TTL on `occurred_at` later).
- Use `motor` (async Mongo client).
- All transitions use `find_one_and_update` with `return_document=AFTER` and a status filter in the query.
- Time fields use `datetime.now(UTC)` server-side in Python; do not rely on Mongo `$currentDate` (test parity).

### `RedisStreamsQueue`

- Stream key per config: `{prefix}:{config_id}:hi` and `{prefix}:{config_id}:lo`.
- Consumer group: `{redis_consumer_group}` (single group per stream).
- Use `redis-py` async client (`redis.asyncio`).
- `enqueue`: `XADD` to `:hi` if priority >= 7 else `:lo`.
- `reserve`: `XREADGROUP` across all listed config streams (both hi and lo), preferring hi via two passes.
- `ack`: `XACK`. `nack(requeue=True)`: `XADD` again (new ID) + `XACK` original.
- `reclaim_stale`: `XAUTOCLAIM` with `idle_ms`.
- `ensure_group`: `XGROUP CREATE … MKSTREAM`, ignore "BUSYGROUP" error.

### `LocalFsArtifactStore`

- Writes to `{artifact_root}/{yyyy}/{mm}/{dd}/{key}`.
- Uses `aiofiles` for async I/O.
- Returns `file://{absolute_path}`.
- `signed_url` returns None.
- `put` computes SHA-256 streamingly and stores it as an extended attribute or a sidecar `.sha256` file.

### `HttpFetchArtifactStore`

- Read-only. `put`/`delete` raise `NotImplementedError`.
- Translates `key` to `{artifact_http_base}/{key}` for `get`/`stat`.
- Used on the control node when reading artifacts the GPU node produced.

### Memory impls

- Pure in-process; no I/O. Used only in tests.
- Must respect protocol semantics including atomicity (use `asyncio.Lock`).

## Conformance test suite

`tests/conformance/` runs the same tests against every backend. Use `pytest.mark.parametrize` over a fixture that yields concrete instances:

```python
@pytest.fixture(params=["memory", "mongo"])
async def state_store(request, mongo_container):
    if request.param == "memory":
        yield MemoryStateStore()
    else:
        yield MongoStateStore(settings_for_test(mongo_container))
```

Required test cases (per protocol):
- **StateStore**: create/get/idempotency hit; transition CAS (precondition wins/loses); lease acquire (only one of N concurrent acquires succeeds); lease extend with wrong `lease_id` returns False; expired lease finder; cancel flag round-trip; artifact + event recording.
- **JobQueue**: enqueue/reserve round-trip; multi-config reserve picks across streams; ack removes; nack requeues; depth correctness; reclaim_stale picks up jobs from a dead consumer.
- **ArtifactStore**: put/get round-trip with bytes and async iterator; stat returns correct size; delete then stat returns None.

## Acceptance criteria

- [ ] `uv run pytest core/` passes (unit + conformance against memory).
- [ ] `uv run pytest core/ -m integration` passes against ephemeral Mongo + Redis (testcontainers fixtures in `conftest.py`).
- [ ] `mypy --strict core/` and `pyright core/` clean.
- [ ] `ruff check core/` and `ruff format --check core/` clean.
- [ ] `from spindle_core import (Job, JobStatus, Worker, ArtifactMeta, ModelConfig, JobEvent, Settings, make_state_store, make_queue, make_artifact_store)` works.
- [ ] No imports from `spindle_api`, `spindle_dispatcher`, `spindle_workers`, `spindle_cli`.

## Out of scope

- ClickHouse / telemetry pipeline (Phase 8).
- Postgres state backend (later swap).
- S3/MinIO artifact backend (later swap).
- NATS queue backend (later swap).
- Eval/replay primitives (Phase 7).
- Auth/RBAC (later).
