# Spindle

A local, durable, observable job fabric for heterogeneous generative ML workloads.

Spindle runs across two or more nodes (a control box and one or more GPU boxes), schedules jobs to the right runtime based on capability and warm-model affinity, persists state across restarts, and keeps enough history to replay/eval jobs against new model configs later.

> **Status: WIP / personal learning project.** No stability guarantees. APIs and schemas may change without notice. Issues and PRs welcome but I'm not committing to fast turnaround — this exists to scratch my own itch and learn the moving parts (schedulers, queues, async Python, S3). See [`STATUS.md`](./STATUS.md) for a live "what works today, what's next" pointer.

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

Three swap-point protocols, all implemented and tested against real backends:

| Concern | Protocol | Backends |
|---|---|---|
| State (jobs / configs / events / artifact metadata) | `StateStore` | `mongo`, in-memory |
| Per-config job queue | `JobQueue` | `redis_streams`, in-memory |
| Artifact bytes | `ArtifactStore` | `s3` (MinIO / AWS / R2 / B2), in-memory |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for components, data flow, and decisions baked in. See [`ROADMAP.md`](./ROADMAP.md) for what gets built when.

## Layout

```
core/         domain types, protocols, backend impls (Mongo / Redis / S3)
api/          FastAPI gateway                          (PLAN.md only)
dispatcher/   host-level scheduler                     (PLAN.md only)
workers/      worker base class + concrete shims       (PLAN.md only)
cli/          `spindle` CLI                             (PLAN.md only)
infra/        Docker compose for MinIO + Mongo + Redis
```

Each component directory has its own `PLAN.md` with goals, scope, file layout, and acceptance criteria — designed so each can be built in isolation against the contracts defined in `core/`.

## Running it locally (today)

What's actually working right now: `core/` only — types, protocols, all three backends, plus the MinIO bootstrap. The API/dispatcher/worker processes don't exist yet (designed in `*/PLAN.md`).

To run the test suite:

```bash
# 1. install uv  (https://docs.astral.sh/uv/)
brew install uv                                 # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. start the backends locally
brew services start mongodb-community@7.0       # macOS
brew services start redis
cd infra/minio && ./bootstrap.sh && cd -        # MinIO via Docker

# 3. install deps + run tests
uv sync --all-packages --all-extras
uv run pytest core/tests                         # ~92 tests, real Mongo + Redis + MinIO

# 4. quality gates
uv run --with ruff ruff check .
uv run --with pyright pyright
```

Configure via env vars (see `.env.example`); copy to `.env` to override defaults.

## Eventually (when the API + dispatcher land)

```bash
cp .env.example .env
make up                                          # docker compose: mongo + redis + minio + api + dispatcher
make worker                                      # cpu_echo worker on the host
spindle submit --type cpu.echo --input '{"message": "hello"}' --watch
```

## License

Apache 2.0. See [`LICENSE`](./LICENSE).
