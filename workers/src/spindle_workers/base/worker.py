"""``WorkerBase`` — abstract long-running worker process.

v0 scope (this commit):
  - Read ``WorkerConfig`` from env.
  - Write a registry descriptor at ``/tmp/spindle-workers/<id>.json``.
  - Heartbeat to stderr every ``heartbeat_seconds``.
  - Handle SIGINT/SIGTERM by removing the registry file and exiting cleanly.
  - Expose ``execute(job, ctx)`` as the abstract method for subclasses.

Out of scope this commit (lands when ``api/`` and ``dispatcher/`` exist):
  - IPC server (Unix socket) for dispatcher-initiated job runs
  - ApiClient with real HTTP heartbeats / lifecycle posts
  - LeaseExtender
  - CancelPoller
  - ArtifactWriter

The shape (``execute`` signature, registry layout, capabilities advertisement)
matches ``workers/PLAN.md`` so adding the missing layers later is additive.
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

from spindle_core.types.artifact import ArtifactMeta
from spindle_core.types.job import Job

from .config import WorkerConfig

log = logging.getLogger("spindle_workers")


@dataclass
class JobResult:
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactMeta] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobContext:
    """What ``execute`` receives.

    v0: just the job. Future fields per ``workers/PLAN.md``:
      - progress: ProgressReporter
      - cancel: CancelToken
      - artifacts: ArtifactWriter
      - extender: LeaseExtender
      - deadline: datetime | None
      - attempt_id: UUID
    """

    job: Job


class WorkerBase(ABC):
    """Subclass to define a worker. Implement ``execute`` and (optionally)
    set ``capabilities``.
    """

    capabilities: list[str] = []

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self._shutdown_event = asyncio.Event()

    @classmethod
    async def boot(cls) -> None:
        """Build config from env and run forever (or until SIGTERM)."""
        config = WorkerConfig.from_env()
        worker = cls(config)
        await worker.run()

    async def run(self) -> None:
        _setup_logging(self.config)
        self._install_signal_handlers()

        log.info(
            "starting worker_id=%s config_id=%s capabilities=%s",
            self.config.worker_id,
            self.config.config_id,
            self.capabilities,
        )

        try:
            await self._register()
            await self._heartbeat_loop()
        finally:
            await self._unregister()
            log.info("exited worker_id=%s", self.config.worker_id)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                # Not supported on this platform (e.g. Windows).
                pass

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

    async def _heartbeat_loop(self) -> None:
        """Idle heartbeat. v0: log only. Real API heartbeat lands later."""
        while not self._shutdown_event.is_set():
            log.info("heartbeat worker_id=%s", self.config.worker_id)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.heartbeat_seconds,
                )
            except asyncio.TimeoutError:
                continue

    @abstractmethod
    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        """Process one job. Subclasses implement.

        v0: this is not yet called by anything (no IPC server, no dispatcher).
        It exists so the shape is locked. ``--test`` mode in concrete worker
        ``main.py`` can call it directly with a synthetic Job for validation.
        """


def _setup_logging(config: WorkerConfig) -> None:
    # Per-worker log already lands in the supervisor's tee. Keep this simple.
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s {config.worker_id} %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
