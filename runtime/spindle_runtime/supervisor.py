"""Supervisor — owns N ChildProcess instances + the embedded dispatcher task.

The supervisor also constructs the dispatcher (if config.dispatcher is set)
and runs it as a peer asyncio task. Both share the same ChildProcess list.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .child import ChildProcess, ChildStatus
from .config import WorkersConfig
from .dispatcher import Dispatcher

log = logging.getLogger("spindle_runtime")


class Supervisor:
    """Top-level process supervisor for one machine's workers + dispatcher."""

    def __init__(self, config: WorkersConfig, logs_dir: Path | None = None) -> None:
        self.config = config
        self.logs_dir = Path(logs_dir).expanduser() if logs_dir else Path(config.log_dir).expanduser()
        self._children: list[ChildProcess] = []
        self._shutdown_event: asyncio.Event | None = None
        self._dispatcher: Dispatcher | None = None
        self._build_children()

    def _build_children(self) -> None:
        for spec in self.config.workers:
            for i in range(spec.replicas):
                worker_id = f"{spec.name}-{i}"
                config_id = spec.env.get("SPINDLE_WORKER_CONFIG_ID")
                self._children.append(
                    ChildProcess(
                        name=spec.name,
                        worker_id=worker_id,
                        module=spec.module,
                        env={**os.environ, **spec.env},
                        logs_dir=self.logs_dir,
                        restart=spec.restart,
                        config_id=config_id,
                        python=spec.python,
                    )
                )

    @property
    def children(self) -> list[ChildProcess]:
        return list(self._children)

    async def run(self) -> None:
        """Spawn children, start the dispatcher task (if configured), wait
        for shutdown signal."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                # Signal handlers not supported on this platform (e.g. Windows).
                pass

        # Build dispatcher lazily so importing spindle-core's backend drivers
        # only happens when actually needed.
        self._dispatcher = self._build_dispatcher_if_configured()

        async with asyncio.TaskGroup() as tg:
            for c in self._children:
                tg.create_task(c.run())
            tg.create_task(self._wait_and_stop())
            if self._dispatcher is not None:
                tg.create_task(self._dispatcher.run())

    def _build_dispatcher_if_configured(self) -> Dispatcher | None:
        if self.config.dispatcher is None:
            log.info("no dispatcher: block in config; supervisor-only mode")
            return None
        try:
            from spindle_core.queue import make_queue
            from spindle_core.settings import Settings
            from spindle_core.state import make_state_store
        except ImportError as e:
            log.warning(
                "dispatcher requested but spindle-core not importable: %s; "
                "running in supervisor-only mode",
                e,
            )
            return None
        settings = Settings()
        return Dispatcher(
            node_id=self.config.node_id,
            children=self._children,
            state=make_state_store(settings),
            queue=make_queue(settings),
            lease_ttl_seconds=self.config.dispatcher.lease_ttl_seconds,
            tick_block_ms=self.config.dispatcher.tick_block_ms,
            configs_override=self.config.dispatcher.configs or None,
        )

    def _request_shutdown(self) -> None:
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def _wait_and_stop(self) -> None:
        assert self._shutdown_event is not None
        await self._shutdown_event.wait()
        if self._dispatcher is not None:
            self._dispatcher.stop()
        await asyncio.gather(
            *(c.stop(self.config.shutdown_grace_seconds) for c in self._children),
            return_exceptions=True,
        )

    def snapshot(self) -> list[ChildStatus]:
        return [c.snapshot() for c in self._children]
