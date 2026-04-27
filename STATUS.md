# Spindle — Status

> One-page "where are we, what's working, what's next" reference. Updated as we go.
> Last commit: 2026-04-26 — `core/queue` + `ModelConfig` + MinIO infra.

## Where we are in one paragraph

Spindle is in active build-out of `core/` (the foundation everyone else depends on). Two of three swap-point protocols are implemented and battle-tested against real backends: **state** (Mongo) and **queue** (Redis Streams). The third — **artifacts** (S3/MinIO) — is the immediate next chunk. Worker and dispatcher process design is paused pending real-runtime learnings (Qwen on Spark, skills enumeration). The repo is private at `github.com/saarang123/spindle`, all quality gates green, 65 tests passing.

---

## Hardware & topology

| Node | Role | Current spec | Storage today | Storage planned |
|---|---|---|---|---|
| **Mac mini M4 Pro** | control plane | 64 GB unified | 512 GB internal | + 2 TB external TB4 (Zike enclosure) |
| **DGX Spark** | GPU + artifact store | 128 GB unified | 4 TB internal NVMe (root) | unchanged |

What runs where (today):

- **Mac mini**: MongoDB (brew services), Redis (brew services), Spindle dev/test loop. *Eventually*: FastAPI gateway, control-side dispatcher, MLX-served text models.
- **Spark**: MinIO (Docker container, healthy, restart=unless-stopped). *Eventually*: GPU-side dispatcher, image/video workers (ComfyUI/diffusers).

Reachability:
- Mac mini → Spark via `spark-8b16:9000` (S3 API), `spark-8b16:9001` (web console). mDNS-resolved, LAN-only.
- SSH alias `spark` → `saarang@spark-8b16` with `~/.ssh/id_ed25519`.

Storage policy (locked):
- **DBs (Mongo, future ClickHouse)** stay on **Mac mini internal NVMe**. External drives = unstable for state stores.
- **Artifacts** live on **Spark MinIO** (4 TB available, room to grow).
- **2 TB external** on Mac mini = models, Docker images, caches, experiments, periodic DB backups. Replaceable stuff only.

---

## Architecture in 60 seconds

```
client → FastAPI (:8080) ─► Mongo (:27017)        ← state of truth
                          ├► Redis (:6379)         ← per-config job streams
                          └► MinIO @ Spark (:9000) ← artifact bytes (S3-compatible)

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
  - Running on Spark, healthy, restart=unless-stopped
  - Credentials in `infra/minio/.env` on Spark (gitignored, persists across reboots)

### Repo / quality

- uv workspace at root, `pyproject.toml` configured for ruff + pyright + pytest
- LICENSE (Apache 2.0), .gitignore, .env.example, .env (local, gitignored)
- 65 tests passing across types + state + queue + configs
- `ruff check` clean, `ruff format --check` clean, `pyright` 0 errors
- All component PLAN.md files written (api, dispatcher, workers, cli, infra)

---

## In flight 🚧

| Track | Owner | Status |
|---|---|---|
| `core/artifacts/` impl (S3 via aioboto3) | this thread | next session, scoped + designed |
| `Worker` + `ArtifactMeta` types in `core` | this thread | bundled with artifacts impl |
| StateStore artifact methods | this thread | bundled with artifacts impl |
| Qwen on Spark (learn caching, runtime shape) | you | active |
| CPU "skill" enumeration | a separate agent | active |

---

## What's left 📋

In rough dependency order:

1. **`core/artifacts/` + Worker/Artifact types** (immediate next chunk)
2. **API** (`api/PLAN.md`) — FastAPI gateway, idempotency, lifecycle endpoints. Doesn't depend on workers.
3. **Dispatcher** (`dispatcher/PLAN.md`) — tick loop, scoring, sweepers, IPC client. Doesn't depend on workers.
4. **Worker base + `cpu_echo`** (`workers/PLAN.md`) — paused; resumes after Qwen + skills agent inform shape.
5. **CLI** (`cli/PLAN.md`) — thin Typer wrapper over API.
6. **Real workers** — MLX text, ComfyUI image/video, CPU pools, external API. Phase 6.
7. **Eval / replay primitives** — shard, replay, score, compare. Phase 7.
8. **ClickHouse telemetry** — async event mirror from state. Phase 8.
9. **Web UI** — far future, optional.

---

## Open decisions / parking lot

Items I've flagged that haven't been answered or that we've explicitly punted:

| Decision | Status | Notes |
|---|---|---|
| Worker process structure (one-config GPU vs many-config CPU pool) | **paused** | Resume after Qwen + skills agent. Schema slot for `Worker.config_ids: list[str]` is locked. |
| `Worker.current_job_ids` field | **dropped** | Volatile state; query `jobs` collection on demand instead |
| Per-job-type input/output schemas | deferred | Lives in `api/` when we build it; conventions documented |
| `runtime_backend` as enum vs string | leaned enum, not yet enforced | Cosmetic, can change anytime |
| `Workflow` model stub | not yet added | Schema slot easy to add later |
| Worker registration: file + API or one of them | leaned both | Decide when worker impl resumes |
| Bucket auto-create in S3ArtifactStore | yes (will implement) | One env var wrong → fail loud |
| ClickHouse retention policy | deferred to Phase 8 | |
| Per-config concurrency caps | deferred | Currently shared per worker |
| YAML-as-source-of-truth for ModelConfig | deferred to CLI (Phase 5) | `spindle config apply` flow |

---

## Open services / running state

Right now (Mac mini):
```
brew services list
# mongodb-community  started   ~/Library/.../mongodb-community.plist
# redis              started   ~/Library/.../redis.plist
```

Right now (Spark, via `ssh spark`):
```
docker ps
# spindle-minio  Up <since bootstrap>  0.0.0.0:9000->9000/tcp, 0.0.0.0:9001->9001/tcp
```

To bring the dev environment up from cold:
```bash
# Mac mini
brew services start mongodb-community@7.0
brew services start redis

# Spark (already running, but to restart)
ssh spark 'cd ~/Documents/spindle/infra/minio && docker compose up -d'

# verify
mongosh --eval "db.runCommand({ping:1}).ok"           # → 1
redis-cli ping                                          # → PONG
curl -fsS http://spark-8b16:9000/minio/health/live -o /dev/null -w "%{http_code}\n"   # → 200
```

To run all tests:
```bash
cd /Users/saarangsrinivasan/Documents/spindle
uv sync --all-packages --all-extras
uv run pytest core/tests             # → 65 passed
uv run --with ruff ruff check .      # → clean
uv run --with pyright pyright        # → 0 errors
```

---

## Where the credentials live

| Secret | Location | Notes |
|---|---|---|
| MinIO root user / password | `~/Documents/spindle/infra/minio/.env` (Spark, gitignored) | also in `.env` on Mac mini for spindle to use |
| Local MongoDB | none (auth disabled, localhost-only) | enable when LAN-exposing for DGX worker eventually |
| Local Redis | none (no requirepass, localhost-only) | same |
| GitHub | `gh` CLI, keychain (Mac) and keyring (Spark) | |

---

## Pointers

- Design / contracts: [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- Phased plan: [`ROADMAP.md`](./ROADMAP.md)
- MinIO setup: [`infra/minio/README.md`](./infra/minio/README.md)
- Per-component plans: `core/PLAN.md`, `api/PLAN.md`, `dispatcher/PLAN.md`, `workers/PLAN.md`, `cli/PLAN.md`, `infra/PLAN.md`
- This file: live status; update as state changes.
