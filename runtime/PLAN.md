# `runtime/` — per-node worker supervisor

A small, per-node Python process that does two things in one place:

1. **Supervises worker child processes** — spawn, restart on crash, drain on shutdown.
2. **Dispatches jobs to local workers** — reads from per-config Redis streams, leases atomically via Mongo, hands off to a local worker over Unix-socket IPC.

Both run as asyncio tasks in the same TaskGroup. They share an in-memory view of "workers on this machine," so the dispatcher doesn't have to re-discover children via the `/tmp/spindle-workers/` registry directory — it reads the supervisor's `ChildProcess` list directly. One runtime instance per machine.

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) section 3 (Components → runtime) and [`../workers/PLAN.md`](../workers/PLAN.md) (workers do not know the runtime exists; they receive jobs via IPC and report lifecycle via the API). The full design rationale for the dispatcher half lives in [`../dispatcher/PLAN.md`](../dispatcher/PLAN.md) — kept as a reference doc; the implementation lives here under `dispatcher.py`.

## Goal

1. **Supervisor**: a YAML-driven loop that spawns N child processes per worker spec, captures their logs, and restarts on exit with exponential backoff.
2. **Dispatcher**: an embedded async task that reads from per-config Redis streams, atomically leases via `StateStore.acquire_lease`, and dispatches jobs to local workers via Unix-socket IPC.
3. A `spindle workers` subcommand group (`run`, `status`, `stop`, `logs`) for operator control.
4. Per-machine deploys: each node has its own YAML; the runtime reads only that file.

The supervisor half is deliberately smaller than supervisord / systemd / launchd — platform-agnostic Python with no extra system dependencies, and a debugging path that lets you bypass it by `uv run python -m spindle_workers.<kind>` with the same env contract. The dispatcher half follows Spindle's planned design (see `../dispatcher/PLAN.md`) but skips sweepers / scoring / recovery in v0.

## Why bundled (tradeoffs)

`dispatcher/PLAN.md` originally specced the dispatcher as its own process. At single-node and two-node scale (Spindle's current deployment surface), two per-node processes (supervisor + dispatcher) bought no real isolation but doubled the operational surface. Folding them into one runtime trades a small amount of failure-domain purity for meaningful ergonomic gains.

**What we gain:**

- **One process per node** instead of two. Single `spindle-workers run` command, single PID, single log stream, single `Ctrl-C`. Half the operational surface to babysit.
- **Shared in-memory worker view.** Dispatcher reads from the supervisor's `ChildProcess` list directly — no polling `/tmp/spindle-workers/<id>.json`, no race between "supervisor wrote descriptor" and "dispatcher reads descriptor."
- **One config file** (the runtime YAML grows a small `dispatcher:` block). No second YAML to keep in sync.
- **Coupled lifecycle.** SIGTERM gracefully stops both halves — dispatcher stops accepting new work, then workers are drained.

**What we accept:**

| Trade-off | Reality at this scale |
|---|---|
| Single failure domain — if dispatcher logic crashes, supervisor goes with it | Dispatcher is small; crashes are bugs to fix, not operational hazards. Worker children are separate processes, untouched by runtime crashes. |
| Can't restart dispatcher independently of supervisor | At 1–2 nodes we redeploy by killing the whole runtime anyway. Worker children come back fast via the restart policy. |
| `spindle-runtime` now depends on `spindle-core` (Mongo + Redis client transitives) | `spindle-core` is small; backends are already running on every node that runs the runtime. |
| Two concerns in one Python package | Cleanly separated in code (`supervisor.py` vs `dispatcher.py`); only bundled at runtime. |

**When we'd un-bundle:**

- Many nodes (10+) where dispatcher cost dominates and per-node failure isolation matters more.
- Frequent dispatcher-logic redeploys (scheduling tweaks) where bouncing workers each time is painful.
- Dispatcher grows complex enough that "different failure domain" becomes a real argument (sweepers, cross-config fairness, etc.).

None of these apply yet. The escape hatch is small: extract `dispatcher.py` into a sibling package, swap the in-process `ChildProcess` lookup for the registry-file path. Bundling is a deployment choice, not a design lock-in.

## Package

`spindle_runtime`.

```
runtime/
  pyproject.toml          # depends on spindle-core (for StateStore + JobQueue)
  src/spindle_runtime/
    __init__.py
    config.py             # YAML schema — RuntimeConfig, WorkerSpec, RestartPolicy,
                          # DispatcherConfig (configs this node handles, lease TTL, …)
    child.py              # ChildProcess — Popen wrapper + restart loop
    supervisor.py         # Supervisor — parses config, owns N ChildProcess instances
    dispatcher.py         # Dispatcher — Redis read, Mongo lease, IPC dispatch task
    ipc_client.py         # Unix-socket JSON-RPC client (dispatcher → worker)
    logging.py            # per-child log routing
    main.py               # `spindle workers <subcommand>` entrypoints (Typer)
  tests/
    test_config.py
    test_child.py
    test_supervisor.py
    test_dispatcher.py    # tick loop, lease race, IPC failure → revert
    conftest.py
```

## Config schema

YAML, one file per machine. Example at [`../configs/runtime.gpu-node.yaml`](../configs/runtime.gpu-node.yaml).

```yaml
node_id: gpu-node
log_dir: ~/.spindle/logs          # per-child log files land here
shutdown_grace_seconds: 10        # SIGTERM, then SIGKILL after this

workers:
  - name: audio-tts-f5
    module: spindle_workers.audio_tts
    replicas: 1
    env:
      SPINDLE_WORKER_CONFIG_ID: audio-tts-f5-v1
      SPINDLE_TTS_BACKEND: f5
    restart:
      policy: always               # always | on_failure | never
      backoff_s: [1, 2, 4, 8, 30]  # capped at last value
      max_consecutive_failures: 0  # 0 = unlimited
```

Pydantic schema (`config.py`):

```python
class RestartPolicy(BaseModel):
    policy: Literal["always", "on_failure", "never"] = "on_failure"
    backoff_s: list[float] = [1, 2, 4, 8, 30]
    max_consecutive_failures: int = 0   # 0 = unlimited

class WorkerSpec(BaseModel):
    name: str                            # base worker name; replicas append "-0", "-1", …
    module: str                          # python module: "spindle_workers.audio_tts"
    replicas: int = 1
    env: dict[str, str] = {}             # injected into child env; SPINDLE_WORKER_ID auto-set
    restart: RestartPolicy = RestartPolicy()

class WorkersConfig(BaseModel):
    node_id: str
    log_dir: Path = Path("~/.spindle/logs").expanduser()
    shutdown_grace_seconds: float = 10.0
    workers: list[WorkerSpec]
```

## Worker ID derivation

For each `WorkerSpec` with `replicas: N`, the supervisor spawns N children with auto-assigned IDs:

```
{spec.name}-{index}        # audio-tts-openai-0, audio-tts-openai-1, …
```

Injected into the child's env as `SPINDLE_WORKER_ID`. The supervisor also sets `SPINDLE_WORKER_IPC_SOCKET` to a per-child path: `/tmp/spindle-worker-{worker_id}.sock`.

User-provided `env` in the YAML MUST NOT include `SPINDLE_WORKER_ID`, `SPINDLE_WORKER_IPC_SOCKET`, or `SPINDLE_LOGS_DIR` — the supervisor owns those. Setting any of them in YAML is a load-time error.

## Logging

All worker output (stdout + stderr) is captured by the supervisor and written to two places:

1. **Per-child log file** at `{logs_dir}/{worker_id}.log`. One file per replica, appended across restarts.
2. **Supervisor stderr** with a `[{worker_id}]` prefix on every line, so a single terminal sees all children interleaved.

The `logs_dir` resolution order, highest precedence first:

1. `--logs-dir` CLI flag passed to `spindle workers run`
2. `SPINDLE_LOGS_DIR` env var (set in the supervisor's environment, not in YAML)
3. `log_dir` field in the YAML config
4. Default: `~/.spindle/logs`

The supervisor injects `SPINDLE_LOGS_DIR={resolved}` into every child's env so workers that want to write structured logs (JSON lines, traces, error dumps) beyond stdout / stderr can find the directory programmatically:

```python
# Inside a worker
import os, json, pathlib
log_dir = pathlib.Path(os.environ["SPINDLE_LOGS_DIR"]) / os.environ["SPINDLE_WORKER_ID"]
log_dir.mkdir(parents=True, exist_ok=True)
(log_dir / "trace.jsonl").open("a").write(json.dumps(event) + "\n")
```

Workers that don't care can just `print` / structlog to stderr — the supervisor still captures it in the per-child file + stderr tee.

`logs_dir` is created on supervisor startup if it doesn't exist (`mkdir -p`). Permissions follow the supervisor process's umask.

Rotation is out of scope for v0. Use `logrotate` on Linux or `newsyslog` on macOS at the supervisor level if files grow. (Or wire `RotatingFileHandler` per child later if it becomes a problem.)

## `ChildProcess`

Lifecycle per replica:

```python
class ChildProcess:
    def __init__(
        self,
        name: str,
        worker_id: str,
        module: str,
        env: dict[str, str],
        log_file: Path,
        restart: RestartPolicy,
    ): ...

    async def run(self) -> None:
        """Spawn → wait → on exit, apply restart policy → repeat. Returns on permanent stop."""

    async def stop(self, grace_seconds: float) -> None:
        """SIGTERM, wait `grace_seconds`, SIGKILL."""

    def snapshot(self) -> ChildStatus:
        """Cheap status snapshot for `spindle workers status`."""
```

Restart loop sketch:

```python
attempt = 0
while not self._stopping:
    self._proc = await asyncio.create_subprocess_exec(
        "uv", "run", "python", "-m", self.module,
        env=self._env_with_id(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    tee_task = asyncio.create_task(self._tee_logs(self._proc.stdout))
    exit_code = await self._proc.wait()
    await tee_task

    if self._stopping:
        return
    if self.restart.policy == "never":
        return
    if self.restart.policy == "on_failure" and exit_code == 0:
        return

    attempt += 1
    if (self.restart.max_consecutive_failures
            and attempt > self.restart.max_consecutive_failures):
        return

    delay = self.restart.backoff_s[min(attempt - 1, len(self.restart.backoff_s) - 1)]
    await asyncio.sleep(delay)
```

Notes:
- A clean exit (code 0) resets the attempt counter under `policy=always`. (v0 keeps the simple monotonic count — fine for now.)
- Logs are written to both a per-child log file at `log_dir/{worker_id}.log` AND tagged-through to the supervisor's own stderr with a `[{worker_id}]` prefix. Dual-write is intentional: `tail -f file` for one child + interactive single-terminal view of everything.

## `Supervisor`

```python
class Supervisor:
    def __init__(self, config: WorkersConfig): ...

    async def run(self) -> None:
        """Spawn all children, wait until all done or signal received."""
        children = [
            ChildProcess(
                name=spec.name,
                worker_id=f"{spec.name}-{i}",
                module=spec.module,
                env={**os.environ, **spec.env},
                log_file=self.config.log_dir / f"{spec.name}-{i}.log",
                restart=spec.restart,
            )
            for spec in self.config.workers
            for i in range(spec.replicas)
        ]

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)

        async with asyncio.TaskGroup() as tg:
            for c in children:
                tg.create_task(c.run())
            tg.create_task(self._await_shutdown_then_stop_all(children))

    def snapshot(self) -> list[ChildStatus]:
        """For `spindle workers status`."""
```

Cooperative shutdown: SIGTERM/SIGINT → mark every `ChildProcess._stopping = True` → send SIGTERM to each child → wait `shutdown_grace_seconds` → SIGKILL stragglers → return.

The supervisor does NOT track jobs, does NOT talk to Mongo / Redis / API. It only knows OS-level process state. Workers report lifecycle to the API independently.

## Dispatcher (embedded)

Runs as a peer asyncio task to the supervisor in the same TaskGroup. Shares the supervisor's in-memory `ChildProcess` list — so the dispatcher knows which local workers exist (and their IPC socket paths) without polling the registry directory.

### Tick loop

Every `tick_ms` (default 200ms):

```python
async def tick(self) -> bool:
    """Returns True if a job was dispatched this tick."""
    active_configs = self._configs_for_this_node()
    if not active_configs:
        return False

    reserved = await self.queue.reserve(
        config_ids=active_configs,
        consumer=self.node_id,
        count=1,
        block_ms=200,
    )
    if not reserved:
        return False

    r = reserved[0]
    job = await self.state.get_job(r.job_id)
    if job is None or job.status != JobStatus.QUEUED:
        await self.queue.ack(r.config_id, r.reservation_id)  # drop; can't run
        return False

    worker = self._pick_local_worker(job.config_id)
    if worker is None:
        await self.queue.nack(r.config_id, r.reservation_id)  # try again next tick
        return False

    lease = await self.state.acquire_lease(
        job.id, worker.worker_id, uuid4(),
        now() + timedelta(seconds=self.lease_ttl_s),
    )
    if lease is None:
        # Race: someone else got it.
        await self.queue.nack(r.config_id, r.reservation_id)
        return False

    try:
        await self.ipc.dispatch(worker.ipc_socket, job, lease)
    except IpcError:
        await self.state.transition(
            job.id, expected_from=JobStatus.LEASED, to=JobStatus.QUEUED,
            patch={"assigned_worker_id": None, "lease_id": None, "lease_expires_at": None},
        )
        await self.queue.nack(r.config_id, r.reservation_id)
        return False

    await self.queue.ack(r.config_id, r.reservation_id)
    return True
```

### Worker selection (v0)

Simplest possible: find the first `ChildProcess` whose worker's `config_id` matches the job's `config_id`. No scoring, no warm-affinity weighting. Add scoring once there's a real reason (multi-worker contention, capacity awareness).

Worker → config_id mapping comes from the YAML's `env: SPINDLE_WORKER_CONFIG_ID` (the same key the worker reads at boot). Cached on the `ChildProcess` so the dispatcher can look it up cheaply.

### IPC client

Length-prefixed JSON over the worker's Unix socket (path: `/tmp/spindle-worker-<worker_id>.sock`). Protocol:

```jsonc
// dispatcher → worker
{ "op": "run", "job_id": "...", "lease_id": "...", "config_id": "...",
  "input": {...}, "deadline_at": "...", "lease_expires_at": "..." }

// worker → dispatcher (immediate ack)
{ "ok": true }
// or:
{ "ok": false, "error": "AT_CAPACITY" }
```

After the immediate ack the worker reports lifecycle to the API directly (`POST /jobs/{id}/start` → ... → `POST /jobs/{id}/complete`). The dispatcher's job ends at hand-off.

### Dispatcher config

Added to the YAML schema as an optional block:

```yaml
node_id: control-node
log_dir: ~/.spindle/logs

dispatcher:
  configs: [audio-tts-openai-v1]   # streams this node reads from Redis
  lease_ttl_seconds: 300           # initial lease length (no extension in v0)
  tick_ms: 200

workers:
  - name: audio-tts-openai
    module: spindle_workers.audio_tts.openai
    replicas: 4
    env:
      SPINDLE_WORKER_CONFIG_ID: audio-tts-openai-v1
```

If `dispatcher` is omitted the runtime runs in supervisor-only mode (useful for nodes that don't need to dispatch — though this is unusual, since the dispatcher *is* the link between the queue and the workers).

### Out of scope (v0)

- Lease sweeper — not needed when leases are 5 min and jobs are short. Add once long-running jobs land.
- Deadline sweeper — same.
- Scoring beyond "first matching local worker" — add when there's contention.
- Startup recovery sweep (re-enqueue `queued` jobs missing from Redis) — add for production deploys.
- Cancellation propagation — add when API exposes `POST /jobs/{id}/cancel`.

## Status surface

Supervisor exposes a small Unix-socket status endpoint at `/tmp/spindle-supervisor-{node_id}.sock`. Three operations:

- `status` → JSON snapshot of all children
- `stop` → request graceful shutdown (same effect as SIGTERM to the supervisor)
- `ping` → reply ok

Used by `spindle workers status` and `spindle workers stop` (see CLI section).

## CLI

```
spindle workers run [--config configs/runtime.<node>.yaml]
    Foreground. Runs until SIGINT/SIGTERM. Ctrl-C kills everything cleanly.

spindle workers status [--config <file>] [--json]
    Snapshot of children: name, pid, uptime, restart count, last exit code.
    Queries the running supervisor via its status socket.

spindle workers stop [--config <file>] [--grace 10]
    Sends `stop` to the running supervisor; falls back to SIGTERM by pidfile.

spindle workers logs <worker_id> [-f] [--config <file>]
    Cats / tails the child's log file from log_dir.
```

If no `--config` is passed, the CLI looks for `SPINDLE_RUNTIME_CONFIG` env var, then `./configs/runtime.{hostname}.yaml`.

`--logs-dir` is also exposed on `spindle workers run` and overrides both `SPINDLE_LOGS_DIR` and the YAML's `log_dir` field. Useful for one-off runs writing to a tmp directory.

## Boot order vs the rest of Spindle

The supervisor does NOT depend on the API being up. Worker children will fail their first heartbeats if the API is down, log retryable errors, and keep trying — that's the worker's concern. The supervisor only cares that the child process exists.

In normal deploys:
1. Start Mongo + Redis (compose).
2. Start the API.
3. Start the dispatcher.
4. `spindle workers run` on each node.

Out-of-order boot is recoverable: workers retry their initial registration.

## Acceptance criteria

- [ ] `uv run pytest runtime/` passes (unit tests against stub child processes).
- [ ] `spindle workers run --config <fixture>` launches N child processes with correct env, all logged to per-child files + stderr tee.
- [ ] Killing a child via SIGKILL triggers restart-with-backoff per policy. Verified with a child that exits on a signal.
- [ ] `spindle workers stop` propagates a stop request and the supervisor exits cleanly within the grace period.
- [ ] `restart.policy = on_failure` with `exit_code = 0` does NOT restart.
- [ ] Manual `python -m spindle_workers.cpu_echo` (bypassing supervisor) still works identically — the supervisor adds no special env that workers depend on.
- [ ] Dispatcher tick: when a fixture enqueues a job for a config that has a local worker, the dispatcher reserves from Redis, leases via Mongo, and writes the `run` payload to that worker's IPC socket (verified with a stub IPC server). Round-trip under 1s.
- [ ] Lease race: when two dispatcher instances try to lease the same job (simulated), exactly one succeeds; the loser nacks the queue and the message is reprocessed.
- [ ] IPC failure: if the worker's socket is dead, the dispatcher reverts the lease (job back to `queued`) and nacks the queue message.
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Cross-node coordination — each supervisor is independent.
- Health checks beyond process-alive — heartbeats live in workers and are observed by the API.
- Resource limits (cgroups, ulimits) — defer to OS or future systemd / launchd integration.
- Hot config reload — restart the supervisor when YAML changes.
- Placement / GPU-aware scheduling — that's the dispatcher's job, not the supervisor's.
- Mac-specific (launchd) or Linux-specific (systemd) deeper integrations — valid for production but explicitly not v0.
- Dispatcher sweepers (lease, deadline, recovery, cancel propagation) — defer until production load demands them.
- Dispatcher scoring beyond "first local worker matching config_id" — same.
- Multi-process dispatcher (sharded reads, multiple consumer groups) — not needed at this scale.
