"""``WorkerBase`` — abstract long-running worker process.

v1 scope (this commit):
  - Read ``WorkerConfig`` from env.
  - Build ``ApiClient`` and ``ArtifactStore`` from settings.
  - Open an IPC server on the configured Unix socket. ``run`` ops trigger
    ``_run_job`` which calls ``execute()``, then posts ``/complete`` or
    ``/fail`` to the API.
  - Write a registry descriptor at ``/tmp/spindle-workers/<id>.json``.
  - Heartbeat to stderr every ``heartbeat_seconds`` (API-side heartbeat
    deferred — runtime owns process liveness).
  - Handle SIGINT/SIGTERM by removing the registry file, closing IPC, and
    exiting cleanly.

Out of scope this commit:
  - LeaseExtender (use long initial leases; revisit when long-running jobs land)
  - CancelPoller (cooperative cancel via API poll; revisit when cancel UX lands)
  - Worker heartbeat to API (runtime / registry directory cover this for now)
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from spindle_core.artifacts import make_artifact_store
from spindle_core.artifacts.protocol import ArtifactStore
from spindle_core.settings import Settings
from spindle_core.types.artifact import ArtifactMeta
from spindle_core.types.errors import ErrorCode, ErrorPayload
from spindle_core.types.job import Job

from .api_client import ApiClient
from .artifact_writer import ArtifactWriter
from .config import WorkerConfig
from .ipc import IpcServer

log = logging.getLogger("spindle_workers")


@dataclass
class JobResult:
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobContext:
    """What ``execute`` receives.

    v1 fields. Deferred (per workers/PLAN.md): progress, cancel, extender,
    deadline.
    """

    job: Job
    artifacts: ArtifactWriter


class WorkerBase(ABC):
    """Subclass to define a worker. Implement ``execute`` and (optionally)
    set ``capabilities``.
    """

    capabilities: list[str] = []

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self._shutdown_event = asyncio.Event()
        self._running_tasks: set[asyncio.Task[None]] = set()

        # Built on .run() so __init__ stays cheap for tests.
        self._api: ApiClient | None = None
        self._artifact_store: ArtifactStore | None = None
        self._ipc: IpcServer | None = None

    @classmethod
    async def boot(cls) -> None:
        """Build config from env and run forever (or until SIGTERM)."""
        config = WorkerConfig.from_env()
        worker = cls(config)
        await worker.run()

    async def run(self) -> None:
        _setup_logging(self.config)
        self._install_signal_handlers()
        self._build_deps()

        log.info(
            "starting worker_id=%s config_id=%s capabilities=%s",
            self.config.worker_id,
            self.config.config_id,
            self.capabilities,
        )

        try:
            await self._register()
            assert self._ipc is not None
            await self._ipc.start()

            ipc_task = asyncio.create_task(self._ipc.serve_forever())
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                await self._shutdown_event.wait()
            finally:
                ipc_task.cancel()
                heartbeat_task.cancel()
                for t in (ipc_task, heartbeat_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                # Wait for in-flight jobs to finish (best-effort, bounded).
                if self._running_tasks:
                    log.info(
                        "waiting for %d in-flight job(s) to finish",
                        len(self._running_tasks),
                    )
                    await asyncio.wait(
                        self._running_tasks,
                        timeout=30.0,
                    )
                if self._ipc is not None:
                    await self._ipc.stop()
        finally:
            if self._api is not None:
                await self._api.close()
            await self._unregister()
            log.info("exited worker_id=%s", self.config.worker_id)

    # ─── setup ───────────────────────────────────────────────────────

    def _build_deps(self) -> None:
        settings = Settings()
        self._api = ApiClient(
            self.config.api_url,
            auth_token=self.config.api_auth_token,
        )
        self._artifact_store = make_artifact_store(settings)
        self._ipc = IpcServer(self.config.ipc_socket, handler=self._handle_ipc)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                pass

    # ─── registry ────────────────────────────────────────────────────

    async def _register(self) -> None:
        self.config.registry_dir.mkdir(parents=True, exist_ok=True)
        registry_file = self.config.registry_dir / f"{self.config.worker_id}.json"
        descriptor = {
            "worker_id": self.config.worker_id,
            "config_id": self.config.config_id,
            "ipc_socket": str(self.config.ipc_socket),
            "capabilities": self.capabilities,
            "concurrency_limit": self.config.concurrency_limit,
            "node": self.config.node,
            "started_at": datetime.now(UTC).isoformat(),
        }
        registry_file.write_text(json.dumps(descriptor, indent=2))
        log.info("registered at %s", registry_file)

    async def _unregister(self) -> None:
        registry_file = self.config.registry_dir / f"{self.config.worker_id}.json"
        try:
            registry_file.unlink(missing_ok=True)
        except OSError as e:
            log.warning("failed to remove registry file: %s", e)

    # ─── heartbeat ───────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Log-only heartbeat for v1. API-side heartbeat lands when the
        runtime needs the API to know about workers (currently it doesn't —
        the runtime tracks process state itself)."""
        while not self._shutdown_event.is_set():
            log.debug("heartbeat worker_id=%s", self.config.worker_id)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                continue

    # ─── IPC handler ─────────────────────────────────────────────────

    async def _handle_ipc(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if op == "ping":
            return {"ok": True}
        if op == "run":
            if len(self._running_tasks) >= self.config.concurrency_limit:
                return {"ok": False, "error": "AT_CAPACITY"}
            # Spawn the job; ack the dispatcher immediately.
            task = asyncio.create_task(self._run_job(msg))
            self._running_tasks.add(task)
            task.add_done_callback(self._running_tasks.discard)
            return {"ok": True}
        if op == "cancel":
            # v1: no cancel token plumbing yet. Acknowledge but do nothing.
            log.warning("cancel op received but not implemented in v1")
            return {"ok": True, "warning": "cancel not implemented"}
        return {"ok": False, "error": f"unknown op: {op!r}"}

    # ─── job execution ───────────────────────────────────────────────

    async def _run_job(self, msg: dict[str, Any]) -> None:
        assert self._api is not None
        assert self._artifact_store is not None

        try:
            job = Job.model_validate(msg["job"])
            lease_id = UUID(msg["lease_id"])
        except (KeyError, ValueError) as e:
            log.error("malformed run message: %s — %r", e, msg)
            return

        attempt_id = uuid4()
        worker_id = self.config.worker_id
        artifact_writer = ArtifactWriter(self._artifact_store, job.id)
        ctx = JobContext(job=job, artifacts=artifact_writer)

        try:
            await self._api.start(job.id, lease_id, worker_id, attempt_id)
        except Exception as e:
            log.exception("failed to POST /jobs/%s/start: %s", job.id, e)
            return

        try:
            result = await self.execute(job, ctx)
        except Exception as e:
            log.exception("execute() raised for job %s", job.id)
            error = ErrorPayload(
                code=ErrorCode.MODEL_RUNTIME_ERROR,
                message=str(e),
                retryable=True,
                details={"exception_type": type(e).__name__},
            )
            try:
                await self._api.fail(job.id, lease_id, worker_id, error=error)
            except Exception:
                log.exception("failed to POST /jobs/%s/fail", job.id)
            return

        # Merge the writer's collected artifacts with anything execute()
        # added explicitly to result.artifacts.
        merged = artifact_writer.collected + result.artifacts
        try:
            await self._api.complete(
                job.id,
                lease_id,
                worker_id,
                output=result.output,
                artifacts=merged,
                runtime=result.runtime,
            )
            log.info("job %s succeeded (%d artifacts)", job.id, len(merged))
        except Exception:
            log.exception("failed to POST /jobs/%s/complete", job.id)

    @abstractmethod
    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        """Process one job. Subclasses implement."""


def _setup_logging(config: WorkerConfig) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s {config.worker_id} %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
