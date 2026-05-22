# `runtime/` — per-node worker supervisor

A small, platform-agnostic process supervisor that boots and babysits worker processes on a single node. One supervisor instance per machine, driven by a YAML config listing every worker on that machine.

Workers themselves are dumb — they self-register via the registry directory, talk to the API directly, and exit on crash. The supervisor's only job is "are my children alive? if not, restart with backoff."

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) section 3 (Components → runtime) and [`../workers/PLAN.md`](../workers/PLAN.md) (workers do not know the supervisor exists). Depends on nothing else in this repo at runtime — uses `asyncio` + `subprocess` only.

## Goal

1. A YAML-driven supervisor that spawns N child processes per worker spec, captures their logs, and restarts on exit with exponential backoff.
2. A `spindle workers` subcommand group (`run`, `status`, `stop`, `logs`) for operator control.
3. Per-machine deploys: each node has its own YAML; the supervisor reads only that file.

Deliberately smaller than supervisord / systemd / launchd. The point is platform-agnostic Python with no extra system dependencies, and a debugging path that lets you bypass the supervisor any time by `uv run python -m spindle_workers.<kind>` with the same env contract.

## Package

`spindle_runtime`.

```
runtime/
  pyproject.toml
  src/spindle_runtime/
    __init__.py
    config.py             # YAML schema (pydantic) — WorkersConfig, WorkerSpec, RestartPolicy
    child.py              # ChildProcess — Popen wrapper + restart loop
    supervisor.py         # Supervisor — parses config, owns N ChildProcess instances
    logging.py            # per-child log routing: file + stderr tee with prefix
    main.py               # `spindle workers <subcommand>` entrypoints (Typer)
  tests/
    test_config.py
    test_child.py
    test_supervisor.py
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
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Cross-node coordination — each supervisor is independent.
- Health checks beyond process-alive — heartbeats live in workers and are observed by the API.
- Resource limits (cgroups, ulimits) — defer to OS or future systemd / launchd integration.
- Hot config reload — restart the supervisor when YAML changes.
- Placement / GPU-aware scheduling — that's the dispatcher's job, not the supervisor's.
- Mac-specific (launchd) or Linux-specific (systemd) deeper integrations — valid for production but explicitly not v0.
