# Spindle

A local, durable, observable job fabric for heterogeneous generative ML workloads.

Runs across a control node + one or more GPU nodes. Schedules jobs to the right runtime based on capability and warm-model affinity. Persists state across restarts. Keeps enough history to replay/eval jobs against new model configs later.

## What it does

- Submit jobs of varied types: `text.*`, `image.*`, `video.*`, `cpu.*`, `external.*`, `eval.*`.
- Track lifecycle (`queued → leased → running → succeeded|failed|canceled|dead_lettered`), retry on transient failures, dead-letter on persistent ones.
- Dispatch to the right worker via per-config queues + a host-level scheduler that scores candidates.
- Persist artifacts (images, video, audio, JSON, text) with lineage back to the producing job and model config.
- Replay historical jobs against alternate model configs for A/B comparison and promotion.

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
core/         domain types, protocols, backend impls (Mongo / Redis / S3)
api/          FastAPI gateway                          (PLAN.md)
dispatcher/   host-level scheduler                     (PLAN.md)
workers/      worker base class + concrete shims       (PLAN.md)
cli/          `spindle` CLI                             (PLAN.md)
infra/        Docker compose, MinIO bootstrap
```

Each component directory has its own `PLAN.md` with goals, scope, file layout, and acceptance criteria.

## Running the test suite

```bash
# install uv  (https://docs.astral.sh/uv/)
brew install uv                                  # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# start the backends
brew services start mongodb-community@7.0        # macOS
brew services start redis
cd infra/minio && ./bootstrap.sh && cd -         # MinIO via Docker

# install deps + run
uv sync --all-packages --all-extras
uv run pytest core/tests                         # ~92 tests, real Mongo + Redis + MinIO
uv run --with ruff ruff check .
uv run --with pyright pyright
```

Configure via env vars; copy `.env.example` to `.env` to override defaults.

## License

Apache 2.0. See [`LICENSE`](./LICENSE).
