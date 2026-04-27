# Spindle — Status

> One-page "where are we, what's working, what's next" reference. Updated as we go.
> Last commit: 2026-04-26 — `core/artifacts` (S3/MinIO).

## Where we are in one paragraph

Spindle is in active build-out of `core/` (the foundation everyone else depends on). All three swap-point protocols are implemented and battle-tested against real backends: **state** (Mongo), **queue** (Redis Streams), and **artifacts** (S3/MinIO). Worker and dispatcher process design is paused pending real-runtime learnings (a real LLM on the GPU node, skills enumeration). 92 tests green, lint/format/pyright clean.

---

## Hardware & topology (reference deployment)

| Node | Role | Spec | Storage |
|---|---|---|---|
| **control node** | API + state plane | ~64 GB RAM, ARM | internal NVMe (DBs only) + optional external SSD (models, Docker, caches) |
| **GPU node** | GPU + artifact store | ~128 GB RAM, NVIDIA GPU | ~4 TB internal NVMe (MinIO + model weights) |

What runs where:

- **control node**: MongoDB, Redis, Spindle dev/test loop. *Eventually*: FastAPI gateway, control-side dispatcher, text models (MLX / llama.cpp / etc.).
- **GPU node**: MinIO (Docker container, healthy, restart=unless-stopped). *Eventually*: GPU-side dispatcher, image/video workers (ComfyUI / diffusers).

Reachability:
- control → GPU via `<gpu-host>:9000` (S3 API), `<gpu-host>:9001` (MinIO web console). LAN-only, mDNS-resolved.
- SSH alias `gpu` (or whatever you set in `~/.ssh/config`) for orchestration.

Storage policy (locked):
- **DBs (Mongo, future ClickHouse)** stay on the **control node's internal NVMe**. External drives can disconnect; state stores can't tolerate that.
- **Artifacts** live on **GPU node MinIO** (largest disk, where outputs are produced).
- **External SSD on control node** (if attached) = models, Docker images, caches, experiments, periodic DB backups. Replaceable stuff only.

---

## Architecture in 60 seconds

```
client → FastAPI (:8080) ─► Mongo (:27017)        ← state of truth
                          ├► Redis (:6379)         ← per-config job streams
                          └► MinIO @ GPU node (:9000) ← artifact bytes (S3-compatible)

dispatcher (one per node)
  reserves from queue → atomic lease via Mongo CAS → dispatches to local worker
  via Unix socket. lease sweeper requeues / dead-letters expired.

workers (one process per ModelConfig, except CPU pools — many config_ids each)
  IPC server accepts dispatch → executes → uploads artifacts via S3 →
  reports lifecycle to API.
```

Three swap points define the architecture:

| Concern | Protocol | v0 backends | Status |
|---|---|---|---|
| Job/event/config metadata | `StateStore` | `mongo` | ✅ done |
| Per-config job queue | `JobQueue` | `redis_streams`, `memory` | ✅ done |
| Artifact bytes | `ArtifactStore` | `s3`, `memory`, `local` | 🚧 next up |

Anyone reading the API/dispatcher/worker code never imports a concrete backend. Swap by changing one env var.

Full design rationale: [`ARCHITECTURE.md`](./ARCHITECTURE.md). Phased build plan: [`ROADMAP.md`](./ROADMAP.md).

---

## What's done ✅

### `core/` — foundation

- **`types/`** — Pydantic domain models
  - `Job`, `JobStatus`, `TERMINAL_STATUSES` — `config_id` is required (locked)
  - `ModelConfig` — mutable upserts; multi-job-type support (locked)
  - `JobEvent`, `JobEventType` — append-only event records
  - `Lease` — return type for atomic acquisition
  - `ErrorCode`, `ErrorPayload`, `RETRYABLE_ERROR_CODES`
- **`state/`** — `StateStore` protocol + `MongoStateStore`
  - Atomic CAS via `find_one_and_update` with pipeline `$ifNull` for first-time-only timestamps
  - Methods: `create_job`, `transition`, `acquire_lease`, `extend_lease`, `request_cancel`, `find_expired_leases`, `find_overdue_jobs`, `record_event`, `list_events`, `upsert_config`, `list_configs`, etc.
  - `validate_on_read` toggle (default true) for hot-path opt-out
  - Indexes: idempotency (partial unique), status, status+type+priority, lease_expires_at, deadline_at, config_id+status, preferred_node+is_active, job_id+occurred_at
- **`queue/`** — `JobQueue` protocol + `RedisStreamsQueue` + `MemoryJobQueue`
  - At-least-once delivery, multi-config blocking reserve, ack/nack-with-requeue, depth, stale reclaim via `XAUTOCLAIM`
  - 12 conformance tests run against both backends — divergence fails CI loudly
- **`settings.py`** — pydantic-settings, env-driven, `.env` support
- **`_time.py`** — `utcnow()` helper (centralized, tz-aware)

### `infra/`

- **`infra/minio/`** — Docker compose service + idempotent bootstrap script + README
  - Running on the GPU node, healthy, restart=unless-stopped
  - Credentials in `infra/minio/.env` on the GPU node (gitignored, persists across reboots)

### Repo / quality

- uv workspace at root, `pyproject.toml` configured for ruff + pyright + pytest
- LICENSE (Apache 2.0), .gitignore, .env.example, .env (local, gitignored)
- 92 tests passing across types + state + queue + configs + artifacts
- `ruff check` clean, `ruff format --check` clean, `pyright` 0 errors
- All component PLAN.md files written (api, dispatcher, workers, cli, infra)

---

## In flight 🚧

| Track | Status |
|---|---|
| Real LLM running on GPU node (learn caching, runtime shape) | active |
| CPU "skill" enumeration (separate agent) | active |

---

## What's left 📋

In rough dependency order:

1. **API** (`api/PLAN.md`) — FastAPI gateway, idempotency, lifecycle endpoints. Doesn't depend on workers.
2. **Dispatcher** (`dispatcher/PLAN.md`) — tick loop, scoring, sweepers, IPC client. Doesn't depend on workers.
3. **Worker base + `cpu_echo`** (`workers/PLAN.md`) — paused; resumes after real-runtime + skills-agent learnings inform shape.
4. **CLI** (`cli/PLAN.md`) — thin Typer wrapper over API.
5. **Real workers** — text (MLX/llama.cpp/etc.), ComfyUI image/video, CPU pools, external API. Phase 6.
6. **Eval / replay primitives** — shard, replay, score, compare. Phase 7.
7. **ClickHouse telemetry** — async event mirror from state. Phase 8.
8. **Web UI** — far future, optional.

---

## Open decisions / parking lot

Items flagged but not yet answered or explicitly punted:

| Decision | Status | Notes |
|---|---|---|
| Worker process structure (one-config GPU vs many-config CPU pool) | **paused** | Resume after real-runtime + skills agent. Schema slot for `Worker.config_ids: list[str]` is locked. |
| `Worker.current_job_ids` field | **dropped** | Volatile state; query `jobs` collection on demand instead |
| Per-job-type input/output schemas | deferred | Lives in `api/` when we build it; conventions documented |
| `runtime_backend` as enum vs string | leaned enum, not yet enforced | Cosmetic, can change anytime |
| `Workflow` model stub | not yet added | Schema slot easy to add later |
| Worker registration: file + API or one of them | leaned both | Decide when worker impl resumes |
| Bucket auto-create in S3ArtifactStore | done | Lazy on first put, idempotent |
| ClickHouse retention policy | deferred to Phase 8 | |
| Per-config concurrency caps | deferred | Currently shared per worker |
| YAML-as-source-of-truth for ModelConfig | deferred to CLI (Phase 5) | `spindle config apply` flow |

---

## Open services / running state

control node:
```
brew services list
# mongodb-community  started
# redis              started
```

GPU node (via SSH):
```
docker ps
# spindle-minio  Up <since bootstrap>  0.0.0.0:9000->9000/tcp, 0.0.0.0:9001->9001/tcp
```

To bring the dev environment up from cold:
```bash
# control node
brew services start mongodb-community@7.0
brew services start redis

# GPU node (already running, but to restart)
ssh <gpu-host> 'cd ~/spindle/infra/minio && docker compose up -d'

# verify
mongosh --eval "db.runCommand({ping:1}).ok"           # → 1
redis-cli ping                                          # → PONG
curl -fsS http://<gpu-host>:9000/minio/health/live -o /dev/null -w "%{http_code}\n"   # → 200
```

To run all tests:
```bash
cd ~/spindle
uv sync --all-packages --all-extras
uv run pytest core/tests             # → 92 passed
uv run --with ruff ruff check .      # → clean
uv run --with pyright pyright        # → 0 errors
```

---

## Where the credentials live

| Secret | Location | Notes |
|---|---|---|
| MinIO root user / password | `~/spindle/infra/minio/.env` on the GPU node (gitignored) | also in `.env` on the control node for Spindle to use |
| Local MongoDB | none (auth disabled, localhost-only) | enable when LAN-exposing |
| Local Redis | none (no requirepass, localhost-only) | same |
| GitHub | `gh` CLI keychain | |

---

## Pointers

- Design / contracts: [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Phased plan: [`ROADMAP.md`](./ROADMAP.md)
- MinIO setup: [`infra/minio/README.md`](./infra/minio/README.md)
- Per-component plans: `core/PLAN.md`, `api/PLAN.md`, `dispatcher/PLAN.md`, `workers/PLAN.md`, `cli/PLAN.md`, `infra/PLAN.md`
- This file: live status; update as state changes.
