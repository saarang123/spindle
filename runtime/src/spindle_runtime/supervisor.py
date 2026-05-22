"""Supervisor — owns N ChildProcess instances, handles signals + shutdown."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from .child import ChildProcess, ChildStatus
from .config import WorkersConfig


class Supervisor:
    """Top-level process supervisor for one machine's workers."""

    def __init__(self, config: WorkersConfig, logs_dir: Path | None = None) -> None:
        self.config = config
        self.logs_dir = Path(logs_dir).expanduser() if logs_dir else Path(config.log_dir).expanduser()
        self._children: list[ChildProcess] = []
        self._shutdown_event: asyncio.Event | None = None
        self._build_children()

    def _build_children(self) -> None:
        for spec in self.config.workers:
            for i in range(spec.replicas):
                worker_id = f"{spec.name}-{i}"
                self._children.append(
                    ChildProcess(
                        name=spec.name,
                        worker_id=worker_id,
                        module=spec.module,
                        env={**os.environ, **spec.env},
                        logs_dir=self.logs_dir,
                        restart=spec.restart,
                        python=spec.python,
                    )
                )

    @property
    def children(self) -> list[ChildProcess]:
        return list(self._children)

    async def run(self) -> None:
        """Spawn every child, wait until shutdown signal arrives."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                # Signal handlers not supported on this platform (e.g. Windows).
                pass

        async with asyncio.TaskGroup() as tg:
            for c in self._children:
                tg.create_task(c.run())
            tg.create_task(self._wait_and_stop())

    def _request_shutdown(self) -> None:
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def _wait_and_stop(self) -> None:
        assert self._shutdown_event is not None
        await self._shutdown_event.wait()
        await asyncio.gather(
            *(c.stop(self.config.shutdown_grace_seconds) for c in self._children),
            return_exceptions=True,
        )

    def snapshot(self) -> list[ChildStatus]:
        return [c.snapshot() for c in self._children]
