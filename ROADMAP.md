# Spindle — Roadmap

Phased build plan. Each phase has a "done when" criterion; don't move on until it's met. Phases are mostly serial, but Phase 2/3/4 can run in parallel once Phase 1 lands.

## Phase 0 — Repo + tooling

- uv workspace at root with sub-packages.
- `pyproject.toml` (root + per package) with deps pinned via `uv lock`.
- ruff config (lint + format), pyright config (strict on `core/`, basic elsewhere).
- pytest + pytest-asyncio + testcontainers (for Mongo + Redis integration tests).
- pre-commit running ruff + pyright.
- GitHub Actions: one workflow that runs lint + type + test on PR + push.
- `Makefile` with `up`, `down`, `logs`, `test`, `lint`, `format`, `worker`.
- `docker/compose.yaml` skeleton (services empty for now).
- `.env.example` documented.
- `LICENSE` (Apache 2.0), `.gitignore`.

**Done when:** `make lint && make test` passes on a clean clone with empty packages.

## Phase 1 — Core (gate for all other work)

Owner: `core/PLAN.md`.

- Domain types as Pydantic models.
- Three protocols (`StateStore`, `JobQueue`, `ArtifactStore`).
- Backends: `mongo` state, `redis` queue, `local` + `http` artifacts. Plus `memory` impl per protocol for tests.
- `Settings` (pydantic-settings) with all env vars from `.env.example`.
- Factories that select backend by env-var token.
- Integration tests against ephemeral Mongo + Redis (testcontainers).

**Done when:** all three protocols pass a shared conformance test suite that runs against every backend, including `memory`. `from spindle_core import make_state_store, make_queue, make_artifact_store` works.

## Phase 2 — API (parallel with 3 + 4 after Phase 1)

Owner: `api/PLAN.md`.

- FastAPI app with all endpoints listed in `api/PLAN.md`.
- Idempotency key handling on submit.
- Worker heartbeat + lifecycle event ingestion.
- Artifact metadata recording + cross-host fetch proxy.
- Structured logging (structlog).
- Health endpoint.

**Done when:** `pytest api/` passes against an in-memory `StateStore` + `JobQueue`. `uvicorn spindle_api.app:app` boots against the docker compose infra.

## Phase 3 — Dispatcher (parallel with 2 + 4 after Phase 1)

Owner: `dispatcher/PLAN.md`.

- Tick loop with reserve → score → lease → dispatch via Unix socket.
- Lease sweeper with retry policy.
- Cancellation propagation.
- Startup recovery sweep (re-enqueue queued jobs missing from queue).
- Configurable node + capability set via env.
- Local worker registry (which workers are alive, where their socket is).

**Done when:** dispatcher running against Mongo + Redis can pick up a job placed in queue by `pytest` fixtures and write it to a stub Unix-socket listener.

## Phase 4 — Workers + cpu_echo (parallel with 2 + 3 after Phase 1)

Owner: `workers/PLAN.md`.

- `WorkerBase` with IPC server, heartbeat loop, lease extension, cancel poll.
- `cpu_echo` worker: sleeps `input.sleep_seconds`, echoes `input.message`, writes a tiny text artifact.
- Worker config loader (yaml + env override).

**Done when:** end-to-end smoke: `spindle submit --type cpu.echo` → API → queue → dispatcher → worker → artifact → API → `spindle status` shows `succeeded`. This is the milestone.

## Phase 5 — CLI

Owner: `cli/PLAN.md`.

- Typer CLI: `submit`, `status`, `cancel`, `workers`, `artifacts`.
- Reads `SPINDLE_API_URL` from env.

**Done when:** all CLI commands hit the API and round-trip cleanly.

## Phase 6 — Real workers

Each is its own subdir under `workers/` and ships independently.

- `workers/text_mlx/` — MLX-backed text generation (Qwen).
- `workers/cpu_ffmpeg/` — ffmpeg wrapper for resize/transcode/concat.
- `workers/image_comfy/` — ComfyUI HTTP client (image gen).
- `workers/video_comfy/` — ComfyUI HTTP client (video gen).
- `workers/external_api/` — HTTP-only worker (voice, publish, analytics).

**Done when:** each worker round-trips its primary job type end-to-end on the target hardware.

## Phase 7 — Eval / replay primitives

- `eval.export_shard` job: query state for jobs matching filters, write JSONL shard manifest.
- `eval.replay` job: fan out replay jobs against alternate model configs, tag with `eval_run_id` + `source_job_id`.
- `eval.score` job: human-in-loop or LLM-judge stub.
- API endpoints for shard creation + listing replays.

**Done when:** can take 100 historical `text.generate` jobs, replay against a second config, and produce a side-by-side comparison record.

## Phase 8 — Telemetry

- ClickHouse schema (`job_events`, `worker_heartbeats`, `scheduler_decisions`, `job_metrics_flat`).
- Async event writer in `core/telemetry/`.
- Buffer-and-retry on insert failure.
- A handful of pre-canned queries for dashboards.

**Done when:** every state transition + scheduler decision lands in ClickHouse within a few seconds, and a sample dashboard renders p50/p95 latency by job type.

## Out of scope, but tracked

- Postgres state backend (when Mongo hurts).
- S3/MinIO artifact backend (when local FS hurts).
- NATS JetStream queue backend (when Redis hurts).
- OpenTelemetry traces alongside ClickHouse events.
- Web UI for history exploration.
- Multi-tenant / quotas / RBAC.
