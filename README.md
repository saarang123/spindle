# Spindle

A local, durable, observable job fabric for heterogeneous generative ML workloads.

Runs across a control node + one or more GPU nodes. Routes each job to a worker on whichever node hosts the matching `ModelConfig`. Persists state across restarts. Keeps enough history to replay / eval jobs against new model configs later.

## What it does

- Submit jobs of varied types: `text.*`, `image.*`, `video.*`, `audio.*`, `cpu.*`, `external.*`, `eval.*`.
- Track lifecycle (`queued → leased → running → succeeded | failed | canceled | dead_lettered`), retry on transient failures, dead-letter on persistent ones.
- Per-config Redis streams + an embedded per-node dispatcher that leases jobs atomically and IPC-dispatches to a local worker. v0 picks the first matching local worker; scoring (warm-model affinity, capacity, fairness) is deferred until contention shows up.
- Persist artifacts (images, video, audio, JSON, text) with lineage back to the producing job and model config.
- Replay historical jobs against alternate model configs for A/B comparison and promotion (Phase 7).

## Data flow in one diagram

```
client  ─POST /jobs─►  api ─►  Mongo (queued)  +  Redis stream (per config_id)
                                                          │
                                                          ▼  (any node serving that config)
                                       runtime: dispatcher tick reads Redis
                                            → acquire_lease (Mongo CAS)
                                            → IPC `op:run` to a local worker's Unix socket
                                                          │
                                                          ▼
                                        worker.execute(job, ctx)
                                            → ArtifactStore.put(...)  → MinIO
                                            → POST /jobs/{id}/complete → api
                                                                       → Mongo (succeeded)

client polls GET /jobs/{id}   → terminal status + artifact URIs
client GET /artifacts/{id}/bytes  → streams from ArtifactStore (cross-node if needed)
```

The dispatcher is **embedded in the runtime** (same process as the supervisor); the API talks to `core`'s state / queue / artifact backends directly, never through Redis or back through itself.

## What it isn't

- Not a distributed scheduler at scale. Designed for ~1k jobs/day across 2–3 nodes.
- Not a workflow/DAG engine. Jobs are independent units. Workflow orchestration lives upstream (agents, scripts, future SDK).
- Not opinionated about runtimes. Workers are thin shims around MLX, vLLM, ComfyUI, ffmpeg, HTTP APIs — Spindle just schedules and observes them.

## Architecture

Three swap-point protocols, each with at least one production backend and an in-memory backend for tests:

| Concern | Protocol | Backends |
|---|---|---|
| State (jobs / configs / events / artifact metadata) | `StateStore` | `mongo`, in-memory |
| Per-config job queue | `JobQueue` | `redis_streams`, in-memory |
| Artifact bytes | `ArtifactStore` | `s3` (MinIO / AWS / R2 / B2), in-memory |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for components and data flow. [`ROADMAP.md`](./ROADMAP.md) lays out what gets built when. [`STATUS.md`](./STATUS.md) tracks current state.

## Layout

```
core/         domain types, protocols, backend impls (Mongo / Redis / S3)   ✓
api/          FastAPI gateway                                                ✓
runtime/      per-node bundle: supervisor + embedded dispatcher              ✓
workers/      WorkerBase + concrete shims (audio_tts: openai/kokoro/f5)      ✓
cli/          `spindle` CLI                                                  (PLAN.md)
infra/        Docker compose, MinIO bootstrap
configs/      sample per-node runtime YAMLs
docs/         consumer-facing API contract
```

Each component directory has its own `PLAN.md` (design reference, locked decisions, acceptance criteria) plus the implementation under `src/`.

## Quickstart: submit a real job

```bash
# install uv  (https://docs.astral.sh/uv/)
brew install uv                                  # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# bring up state + queue + artifacts
brew services start mongodb-community@7.0        # macOS
brew services start redis
cd infra/minio && ./bootstrap.sh && cd -         # MinIO via Docker

# install workspace + the extras you actually need on this node
# (kokoro / f5 pull torch — install them only on a GPU node)
uv sync --all-packages --extra audio_tts --extra dev

# start the API (control node)
uv run spindle-api &

# seed a ModelConfig
curl -X POST http://localhost:8080/configs -H 'Content-Type: application/json' \
  -d '{"id":"audio-tts-openai-v1","name":"OpenAI TTS","version":"v1",
       "job_types":["audio.tts"],"preferred_node":"control-node",
       "runtime_backend":"openai","model_ref":"tts-1-hd","is_active":true}'

# start the runtime (supervisor + dispatcher + N worker processes)
uv run spindle-workers run --config configs/runtime.control-node.yaml &

# submit a job
curl -X POST http://localhost:8080/jobs -H 'Content-Type: application/json' \
  -d '{"type":"audio.tts","config_id":"audio-tts-openai-v1",
       "input":{"text":"Hello world."}}'

# poll
curl http://localhost:8080/jobs/<id>
```

## Running the test suite

```bash
# install deps for the tests you want
uv sync --all-packages --extra dev --extra audio_tts

# unit + smoke tests across components (~36 + 92 core tests)
uv run pytest core/tests api/tests runtime/tests workers/tests

uv run --with ruff ruff check .
uv run --with pyright pyright
```

`--all-extras` pulls every backend; on Python 3.14 kokoro's transitive `spacy` dep doesn't have wheels — pick extras per host instead.

Configure via env vars; copy `.env.example` to `.env` to override defaults.

## License

Apache 2.0. See [`LICENSE`](./LICENSE).
