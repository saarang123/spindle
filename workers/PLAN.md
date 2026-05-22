# `workers/` — worker base + concrete shims

A **worker** is a long-running process bound to one model config. It listens on a Unix socket for job dispatches from the local dispatcher, executes the job (usually by calling a model server's HTTP API), and reports lifecycle events to the API.

Workers are intentionally dumb. The dispatcher decides *what* to run; the worker just runs it.

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) sections 3 (Components → workers), 4 (Data flow → execute step), and 6 (Key decisions → workers as shims). Depends on [`../core/PLAN.md`](../core/PLAN.md) and [`../api/PLAN.md`](../api/PLAN.md) (workers POST to the API).

## Goal

1. A `WorkerBase` class in `workers/base/` that handles the lifecycle plumbing (IPC server, heartbeat, lease extension, cancel polling, lifecycle event posting, artifact uploading).
2. A reference worker `cpu_echo` that proves the end-to-end flow: dispatcher → worker → artifact → API → status `succeeded`.
3. Phase 6 ships real workers (MLX text, ComfyUI image/video, ffmpeg) — out of scope here.

## Package

`spindle_workers`. Concrete workers each get their own subpackage so they can be installed independently (with optional ML deps).

```
workers/
  pyproject.toml                          # base + cpu_echo only; real workers add deps later
  src/spindle_workers/
    __init__.py
    base/
      __init__.py
      worker.py                           # WorkerBase abstract
      ipc_server.py                       # Unix-socket JSON RPC server
      heartbeat.py                        # heartbeat loop
      lifecycle.py                        # API client wrapper for /jobs/{id}/{start,progress,...}
      lease_extender.py                   # background lease-extend loop
      cancel_poller.py                    # async iterator yielding cancel events
      artifact_writer.py                  # ArtifactStore + register-with-API helper
      registry_register.py                # writes /tmp/spindle-workers/<id>.json on boot
      config.py                           # WorkerConfig (yaml + env)
      logging.py
      main.py                             # generic entrypoint (see "Running" below)
    cpu_echo/
      __init__.py
      worker.py                           # CpuEchoWorker(WorkerBase)
      main.py                             # `python -m spindle_workers.cpu_echo`
    audio_tts/                            # see ./audio_tts/PLAN.md
      __init__.py
      worker.py                           # AudioTtsWorker(WorkerBase)
      main.py                             # `python -m spindle_workers.audio_tts`
      backends/                           # BaseTTS + openai/f5/kokoro impls
  tests/
    test_ipc_server.py
    test_lease_extender.py
    test_cpu_echo.py
    conftest.py
```

Real-runtime workers (later phases) live as siblings: `text_mlx/`, `image_comfy/`, `video_comfy/`, `cpu_ffmpeg/`, `external_api/`. Each subpackage owns its own `pyproject.toml`-style optional-deps marker.

## `WorkerBase`

```python
class WorkerBase(ABC):
    config: WorkerConfig
    api: ApiClient
    artifacts: ArtifactStore

    def __init__(self, config: WorkerConfig, deps: WorkerDeps): ...

    async def run(self) -> None:
        """Main entrypoint. Boots IPC server, heartbeat, sweepers; runs forever."""
        async with TaskGroup() as tg:
            tg.create_task(self._heartbeat_loop())
            tg.create_task(self._ipc_server())
            await self._registry_announce()
            await self._await_shutdown()

    @abstractmethod
    async def execute(self, job: Job, ctx: JobContext) -> JobResult: ...

    # ----- internal -----

    async def _handle_dispatch(self, msg: dict) -> None:
        """Called by IPC server on receiving an `op: run`. Spawns _run_job."""
        ...

    async def _run_job(self, job: Job, lease: Lease) -> None:
        ctx = JobContext(
            job=job, lease=lease,
            progress=ProgressReporter(self.api, job.id, lease.id, self.config.worker_id),
            cancel=CancelToken(self.api, job.id),
            artifacts=ArtifactWriter(self.artifacts, self.api, job.id),
            extender=LeaseExtender(self.api, job.id, lease.id, self.config.worker_id),
            deadline=job.deadline_at,
        )
        await self.api.start(job.id, lease.id, self.config.worker_id, attempt_id=ctx.attempt_id)
        async with ctx.extender.running():
            try:
                result = await self.execute(job, ctx)
            except asyncio.CancelledError:
                await self.api.fail(job.id, lease.id, self.config.worker_id, error=ErrorPayload(
                    code=ErrorCode.WORKER_LOST, message="canceled", retryable=False,
                ))
                raise
            except Exception as e:
                await self.api.fail(job.id, lease.id, self.config.worker_id,
                    error=self._classify_error(e))
                return
        await self.api.complete(job.id, lease.id, self.config.worker_id,
            output=result.output, artifacts=result.artifacts, runtime=result.runtime)
```

### `JobContext`

What `execute` gets:

```python
@dataclass
class JobContext:
    job: Job
    lease: Lease
    progress: ProgressReporter         # await progress.report(phase=..., step=..., total_steps=...)
    cancel: CancelToken                # await cancel.check() → bool; cancel.raise_if_set()
    artifacts: ArtifactWriter          # await artifacts.write(key, bytes, kind=..., metadata=...)
    extender: LeaseExtender
    deadline: datetime | None
    attempt_id: UUID
```

### `ProgressReporter`

Calls `POST /jobs/{id}/progress`. Returns the API's `cancel_requested` flag from the response so workers can check and bail out without a separate request.

### `CancelToken`

Cheap polling against `GET /jobs/{id}/cancel_status`. Workers in long sampler loops should call `cancel.raise_if_set()` between iterations. Default poll cadence: 2s, configurable via `SPINDLE_WORKER_CANCEL_POLL_SECONDS`.

Also receives `op: cancel` IPC messages from the dispatcher and trips immediately.

### `LeaseExtender`

Background task that calls `POST /jobs/{id}/extend_lease` at half the lease TTL (e.g., every 30s for a 60s lease). On API failure, logs but keeps trying (the dispatcher will recover via lease sweeper if extension fails and lease expires).

### `ArtifactWriter`

```python
async def write(
    self, key: str, data: bytes | AsyncIterator[bytes], *,
    kind: ArtifactKind, mime_type: str | None = None,
    width: int | None = None, height: int | None = None, ...,
) -> ArtifactMeta:
    uri = await self.store.put(f"{self.job_id}/{key}", data, content_type=mime_type)
    meta = ArtifactMeta(id=uuid4(), job_id=self.job_id, kind=kind, uri=uri, ...)
    # write metadata via API so it survives even if worker crashes after upload
    await self.api.record_artifact(meta)
    return meta
```

Note: artifact metadata flows to state via the API, NOT directly via `StateStore` from the worker. Workers do not have DB credentials; only the API does.

## IPC server

Unix socket at `SPINDLE_WORKER_IPC_SOCKET`. Length-prefixed (4-byte big-endian) JSON. Asyncio-based.

Operations:
- `run` — start a job in a new task. Reply `{"ok": true}` immediately, then run async.
- `cancel` — trip the cancel token for a running job_id. Reply `{"ok": true}`.
- `ping` — reply `{"ok": true}`.

Reject `run` if `concurrency_used >= concurrency_limit` with `{"ok": false, "error": "AT_CAPACITY"}` so the dispatcher can mark the worker busy and try a different one.

## Registry announce

On boot, write a JSON descriptor to `/tmp/spindle-workers/<worker_id>.json` (path configurable). On clean shutdown, delete it. Dispatcher tails this directory.

```json
{
  "worker_id": "control-text-0",
  "config_id": "qwen-text-v1",
  "ipc_socket": "/tmp/spindle-worker-text-0.sock",
  "concurrency_limit": 4,
  "started_at": "..."
}
```

## Heartbeat

Every `SPINDLE_WORKER_HEARTBEAT_SECONDS` (default 10s), POST `/workers/{id}/heartbeat` with current state. Skipping a heartbeat is OK; dispatchers and the API treat absence > N seconds as "stale".

## Configuration

Each worker process reads:
- `SPINDLE_WORKER_ID` — required. Logical worker name.
- `SPINDLE_WORKER_CONFIG_ID` — required. The `ModelConfig` this worker is bound to.
- `SPINDLE_WORKER_IPC_SOCKET` — Unix socket path.
- `SPINDLE_API_URL` — for lifecycle posts.
- Per-worker YAML: `configs/worker_<id>.yaml` for runtime-specific knobs (model path, batch size, etc.). Loaded via `WorkerConfig.from_yaml(path)`. Env vars override.

## `cpu_echo` reference worker

```python
class CpuEchoWorker(WorkerBase):
    capabilities = ["cpu.echo"]

    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        sleep_s = float(job.input.get("sleep_seconds", 0.5))
        message = str(job.input.get("message", ""))

        for i in range(10):
            ctx.cancel.raise_if_set()
            await asyncio.sleep(sleep_s / 10)
            await ctx.progress.report(phase="echoing", step=i + 1, total_steps=10)

        text = f"echo: {message}"
        artifact = await ctx.artifacts.write(
            key="output.txt", data=text.encode(), kind=ArtifactKind.TEXT, mime_type="text/plain",
        )

        return JobResult(
            output={"text": text, "echoed": message},
            artifacts=[artifact],
            runtime={"execution_ms": int(sleep_s * 1000)},
        )
```

`python -m spindle_workers.cpu_echo` boots it.

## Running

In production, workers are launched by the runtime supervisor (see [`../runtime/PLAN.md`](../runtime/PLAN.md)):

```bash
spindle workers run --config configs/runtime.<node>.yaml
```

For debugging or one-off runs, the same worker can be launched manually with the env vars the supervisor would inject:

```bash
SPINDLE_WORKER_ID=control-echo-0 \
SPINDLE_WORKER_CONFIG_ID=cpu-echo-v1 \
SPINDLE_WORKER_IPC_SOCKET=/tmp/spindle-worker-control-echo-0.sock \
uv run python -m spindle_workers.cpu_echo
```

This is identical to what the supervisor does internally, so the supervisor never becomes a debugging fog — bypass it any time. Real workers (`audio_tts`, future `text_mlx`, etc.) follow the same pattern.

A `ModelConfig` for `cpu-echo-v1` should be seeded by infra startup (so the dispatcher knows about it). Seed JSON lives in `infra/seed/configs/cpu_echo.yaml` — loaded by an init script at compose-up time or a CLI command (`spindle config apply <yaml>`).

## Acceptance criteria

- [ ] `uv run pytest workers/` passes (unit tests for IPC server, lifecycle plumbing, lease extender against an `ApiClient` fixture).
- [ ] `cpu_echo` worker round-trips a `cpu.echo` job end-to-end: IPC accept → progress reports → artifact upload → API complete → state shows `succeeded` with the artifact attached.
- [ ] Cancellation via IPC trips the worker mid-job and produces a `failed` (or specifically a worker-side abort that the API handles as canceled).
- [ ] Lease extension fires at half-TTL; worker survives a 30s lease for a 50s job.
- [ ] No imports from `spindle_dispatcher`. Workers may import `spindle_core` and use an HTTP client to talk to the API.
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Real model runtimes (MLX, ComfyUI, vLLM, ffmpeg, diffusers). Phase 6.
- Worker-side queue consumption (workers never read from Redis directly).
- Multi-config workers in one process (one process = one config). Multiple processes of the *same* config are supported — that's the runtime supervisor's `replicas` field, not the worker's concern.
- Hot reload of the worker config without restart.
