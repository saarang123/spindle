# Spindle

A local, durable, observable job fabric for heterogeneous generative ML workloads.

Spindle runs across two or more nodes (a control box and one or more GPU boxes), schedules jobs to the right runtime based on capability and warm-model affinity, persists state across restarts, and keeps enough history to replay/eval jobs against new model configs later.

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

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for components, data flow, and decisions baked in.

See [`ROADMAP.md`](./ROADMAP.md) for what gets built when.

## Layout

```
core/         domain types, protocols, backend impls (Mongo / Redis / local-FS)
api/          FastAPI gateway
dispatcher/   host-level scheduler — one process per node
workers/      worker base class + concrete worker shims
cli/          `spindle` CLI for submitting and inspecting jobs
infra/        Docker compose + Makefile
docs/         extended notes
```

Each component directory has its own `PLAN.md` with goals, scope, file layout, and acceptance criteria — designed so each can be built in isolation against the contracts defined in `core/`.

## Quickstart

Pending Phase 0 completion. Target shape:

```bash
cp .env.example .env
make up          # mongo + redis + clickhouse + api in docker
make worker      # cpu_echo worker on the host
spindle submit --type cpu.echo --input '{"message": "hello"}'
spindle status <job_id>
```

## License

Apache 2.0. See [`LICENSE`](./LICENSE).
