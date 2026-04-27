# Spindle — Architecture

This is the design source of truth. Every component PLAN.md references this document for context. If you change the design, update this file first, then propagate.

## 1. Overview

Spindle is a two-or-more-node job fabric. One node runs the control plane (API, state DB, queue, telemetry). One or more nodes run heavy GPU workers. Jobs flow:

```
client → api → state(queued) + queue.enqueue(config_id, job_id)
                                  │
                                  ▼
                    host_dispatcher reserves from queue
                                  │
                       atomic state.acquire_lease
                                  │
                                  ▼
                    dispatcher → local worker (Unix socket)
                                  │
                                  ▼
                       worker → model server (HTTP)
                                  │
                                  ▼
                       worker → artifact_store.put + api.complete
                                  │
                                  ▼
                              state(succeeded)
```

Three things are intentionally swappable behind protocols: the **state store**, the **job queue**, and the **artifact store**. Everything else is concrete.

## 2. Topology

The reference deployment is a **control node + GPU node**, but no code hardcodes that. Nodes are arbitrary strings; capabilities and resource pools do the work.

```
┌─────────────────── control node (~64 GB RAM) ──────────────────────┐
│  FastAPI gateway        :8080 (LAN-bound)                          │
│  MongoDB                :27017 (LAN-bound, auth required)          │
│  Redis                  :6379  (LAN-bound, auth required)          │
│  ClickHouse             :8123  (LAN-bound)             [later]     │
│  control-dispatcher     (one process)                              │
│  text worker            (MLX / llama.cpp / etc.)                   │
│  cpu worker pool        (ffmpeg, external API jobs)                │
└─────────────────────────────────────────────────────────────────────┘
                                │  ethernet LAN
                                │  mDNS or /etc/hosts
                                ▼
┌─────────────────── GPU node (~128 GB RAM) ─────────────────────────┐
│  gpu-dispatcher         (one process)                              │
│  image worker           (diffusers / comfy client)                 │
│  video worker           (comfy / CLI client)                       │
│  MinIO :9000            S3-compatible artifact store               │
│                         backed by local NVMe                       │
└─────────────────────────────────────────────────────────────────────┘
```

Network: same Ethernet, no internet exposure. Auth is username/password on Mongo+Redis, optional bearer token on the API. TLS deferred until something is on a coffee-shop network.

## 3. Components

### `core/`

The foundation. Defines:

- Domain types (`Job`, `Worker`, `ArtifactMeta`, `Lease`, `ModelConfig`, event types) as Pydantic models.
- Three protocols: `StateStore`, `JobQueue`, `ArtifactStore`.
- Concrete backends: Mongo state, Redis Streams queue, local-FS + HTTP-fetch artifact stores. Plus in-memory variants for tests.
- `Settings` (pydantic-settings) — single source of env-driven config.
- Factory functions that select backends by env-var token and lazy-import drivers.

No HTTP server, no scheduling logic, no model-runtime imports. Everything else depends on this; this depends on nothing in the repo.

### `api/`

FastAPI gateway. The only HTTP-facing service. Owns:

- Job submission (with idempotency).
- Job status / cancel.
- Worker registration + heartbeat ingestion.
- Worker-reported lifecycle events (start, progress, complete, fail, extend lease).
- Artifact metadata recording + cross-host artifact fetch proxy.

API does **not** do leasing — that's the dispatcher. API talks to `StateStore` and `JobQueue` directly.

### `dispatcher/`

One process per host. Owns:

- Tick loop: reserve from per-config Redis streams (only configs assigned to this host) → score candidates → atomic lease via `StateStore.acquire_lease` → hand off to local worker via Unix socket.
- Lease sweeper: periodically find expired leases and either requeue or dead-letter based on retry policy.
- Cancellation propagation: read `cancel_requested` flag from state, signal local worker.
- Configurable: `SPINDLE_DISPATCHER_NODE` and a list of configs/capabilities this dispatcher serves.

Dispatcher talks directly to `StateStore` and `JobQueue`. It does NOT go through the API — the API is for clients and workers.

### `workers/`

Worker base class + concrete worker shims.

`WorkerBase` handles:
- Local IPC server (Unix socket) that accepts "run this job" from the dispatcher.
- Heartbeat loop → POST `/workers/{id}/heartbeat`.
- Lease extension during long jobs.
- Cancellation polling.
- Reporting start / progress / complete / fail to the API.
- Artifact uploads via `ArtifactStore`.

Concrete workers subclass and implement `async def execute(job, ctx) -> WorkerOutput`. Phase-4 ships `cpu_echo` (sleeps and round-trips). Real runtimes (MLX, ComfyUI, ffmpeg) come in Phase 6.

Workers do not import from `dispatcher/` or `api/`. They depend on `core/` only.

### `cli/`

Thin Typer-based CLI hitting the API. `spindle submit`, `spindle status`, `spindle workers`, `spindle cancel`. No business logic; all server-side.

### `infra/`

Docker Compose for the control node (Mongo, Redis, ClickHouse, API, control-dispatcher). Dockerfiles for API and dispatcher. Makefile targets. GPU-node services run native (no Docker — CUDA/MPS passthrough is too painful for v0).

## 4. Data flow — job lifecycle

1. **Submit**. `POST /jobs` → API validates, looks up `model_config_id`, picks the target node from config, calls `state.create_job(status=queued)`, then `queue.enqueue(config_id, job_id)`. Returns `job_id`. If queue enqueue fails, state stays at `queued`; the dispatcher's startup recovery sweep will re-enqueue it.
2. **Reserve**. The host dispatcher's tick loop calls `queue.reserve([active_config_ids], consumer="<node>-dispatcher", count=N)`. Redis Streams XREADGROUP returns one or more messages.
3. **Score & lease**. Dispatcher scores reserved candidates (priority + warm-model affinity + concurrency headroom). Picks the best. Calls `state.acquire_lease(job_id, worker_id, lease_id, expires_at)` — atomic CAS on `status='queued'`. If it fails (someone else took it), `queue.nack` and continue. If it succeeds, `queue.ack`.
4. **Dispatch**. Dispatcher writes the job to the local worker's Unix socket: `{"action": "run", "job_id": ..., "lease_id": ..., "input": ..., "deadline_at": ...}`. Worker accepts.
5. **Execute**. Worker POSTs `/jobs/{id}/start`, calls the model server, streams progress (`POST /jobs/{id}/progress`), extends lease as needed (`POST /jobs/{id}/extend_lease`), polls cancel flag.
6. **Artifact**. On completion, worker writes bytes via `artifact_store.put`, gets a URI, then calls `POST /jobs/{id}/complete` with the URI + metadata. API records artifact metadata in state and transitions job to `succeeded`.
7. **Telemetry**. Each transition emits an event. Phase-8 wires these to ClickHouse; until then, structured logs.

Failure paths:
- Worker crash → lease expires → sweeper requeues (if retryable + retries left) or dead-letters.
- Deadline hit → sweeper transitions job to `failed` with `DEADLINE_EXCEEDED`, signals worker to abort.
- Non-retryable error from worker → `POST /jobs/{id}/fail` with `retryable=false` → state goes to `failed`, no requeue.

## 5. Swap points

Three protocols in `core/`. Each implementation is a folder with a single class. Selection is one env var.

| Concern | Protocol | v0 backends | Selected by |
|---|---|---|---|
| Job/worker/artifact metadata | `StateStore` | `mongo`, `memory` | `SPINDLE_STATE_BACKEND` |
| Per-config queue | `JobQueue` | `redis`, `memory` | `SPINDLE_QUEUE_BACKEND` |
| Artifact bytes | `ArtifactStore` | `local`, `http` | `SPINDLE_ARTIFACT_BACKEND` |

Postgres state, NATS queue, S3/MinIO artifacts can land later as new files; no other code changes.

Every protocol method has documented semantics for atomicity, blocking behavior, and idempotency. Backends must satisfy them or test failures will catch it (memory impl is the reference).

## 6. Key decisions

**Mongo, not Postgres, for v0.** Schema-loose JSON inputs/outputs fit naturally. `findOneAndUpdate` is atomic enough for lease acquisition. Postgres is a swap-point file later if we hit a wall.

**Per-config Redis Streams, not one global queue.** Lets dispatchers subscribe only to configs assigned to their host. Inactive configs don't drop jobs — they just queue. New configs need provisioning (one stream key); we tolerate that.

**Host dispatcher, not per-worker queue consumers.** Single decision point per host enables smart scheduling (image before video, defer when memory pressure, warm-model preference). Workers stay dumb shims.

**Workers are shims around model servers, not queue consumers.** Real model runtimes (vLLM, ComfyUI, MLX server, diffusers) already expose HTTP. Workers translate "run this job" to "call this HTTP endpoint" and report back. Swap a runtime by rewriting the shim, nothing else.

**Artifact storage on the GPU node.** 4TB live there; videos are 10s–100s of MB. Keep bytes on the node that produces them. Control node fetches via NFS or a tiny HTTP endpoint when needed.

**Dispatcher dispatches to workers via local IPC, not HTTP.** No serialization overhead, no network, simpler failure modes. Unix socket on macOS/Linux.

**Telemetry async + lossy-tolerant.** ClickHouse writes never block job execution. Buffer locally on insert failure. Source of truth stays in Mongo.

**Idempotency via client-supplied key on submission.** API checks `state.find_by_idempotency_key(key)` before creating; returns existing job_id if hit.

**Cancellation is cooperative.** Workers poll a `cancel_requested` flag every 2s. GPU loops check between sampler steps. Hard kills only on lease expiry sweeper.

**No DAG/workflow engine.** Jobs are independent. Workflow orchestration is an upstream concern (Claude/Codex agents, scripts). Job records have a `workflow_id` field for grouping/filtering, but we don't resolve dependencies between them.

## 7. Non-goals (for now)

- Multi-tenant isolation, quotas, RBAC.
- Distributed scheduler across >5 nodes.
- Hot model swap with zero downtime (we accept "drain queue, swap, resume").
- Workflow DAG resolution (deferred to upstream agents).
- ClickHouse-fronted dashboard UI (Phase 8+).
- Public PyPI distribution.

## 8. Glossary

- **Job**: a single unit of work with a type, input, config, and lifecycle. Independent.
- **Worker**: a long-running process bound to one model config. Shim that calls a model server.
- **Node**: a physical machine. Identified by string.
- **Capability**: a job type a worker can execute (e.g., `text.generate`, `image.generate`).
- **Model config / job config**: a named runtime configuration — model weights, backend, default params, target node. Jobs reference one.
- **Lease**: a time-bounded claim on a job by a worker. Extended periodically. Expiry triggers requeue or dead-letter.
- **Artifact**: a file produced by a job. Stored as bytes by `ArtifactStore`; metadata in `StateStore`.
- **Dispatcher**: the host-level scheduler process. One per node.
- **Trace**: historical record (state + telemetry) sufficient to replay a job.
- **Shard**: a saved filtered subset of historical jobs, used as input to eval/replay.
