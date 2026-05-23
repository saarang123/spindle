"""Embedded dispatcher task — runs alongside the supervisor in the runtime.

Reads from per-config Redis streams, atomically leases via Mongo, IPC-dispatches
to local workers. The supervisor's ``ChildProcess`` list is the source of truth
for "which workers exist on this machine" — no ``/tmp/spindle-workers/`` polling.

v0 scope: tick + lease + dispatch. Out: lease sweeper, deadline sweeper,
recovery sweep at startup, cancel propagation, scoring beyond "first match".
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from spindle_core.queue.protocol import JobQueue
from spindle_core.state.protocol import StateStore
from spindle_core.types.job import Job, JobStatus

from .child import ChildProcess

log = logging.getLogger("spindle_runtime")


class Dispatcher:
    """One-tick-at-a-time job router for the local node."""

    def __init__(
        self,
        *,
        node_id: str,
        children: list[ChildProcess],
        state: StateStore,
        queue: JobQueue,
        lease_ttl_seconds: float = 300.0,
        tick_block_ms: int = 200,
        configs_override: list[str] | None = None,
    ) -> None:
        self.node_id = node_id
        self._children = children
        self._state = state
        self._queue = queue
        self._lease_ttl_seconds = lease_ttl_seconds
        self._tick_block_ms = tick_block_ms
        self._configs_override = configs_override
        self._stopping = asyncio.Event()

    def active_configs(self) -> list[str]:
        """Configs this dispatcher serves.

        Explicit override wins if non-empty; otherwise derived from the union
        of local children's ``config_id`` values.
        """
        if self._configs_override:
            return list(self._configs_override)
        seen: set[str] = set()
        for c in self._children:
            if c.config_id:
                seen.add(c.config_id)
        return sorted(seen)

    async def run(self) -> None:
        configs = self.active_configs()
        if not configs:
            log.warning(
                "dispatcher: no configs to serve (no local workers with "
                "SPINDLE_WORKER_CONFIG_ID); idling until shutdown"
            )
            await self._stopping.wait()
            return

        log.info("dispatcher: serving configs=%s", configs)
        # Make sure consumer groups + streams exist before the first read.
        for cfg in configs:
            try:
                await self._queue.ensure_group(cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("ensure_group(%s) failed: %s", cfg, e)

        while not self._stopping.is_set():
            try:
                await self._tick(configs)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatcher tick crashed; sleeping 1s")
                await asyncio.sleep(1.0)

    def stop(self) -> None:
        self._stopping.set()

    async def _tick(self, configs: list[str]) -> bool:
        """One reserve → score → lease → dispatch cycle. Returns True if a
        job was dispatched."""
        reserved = await self._queue.reserve(
            config_ids=configs,
            consumer=self.node_id,
            count=1,
            block_ms=self._tick_block_ms,
        )
        if not reserved:
            return False

        r = reserved[0]

        job = await self._state.get_job(r.job_id)
        if job is None or job.status != JobStatus.QUEUED:
            # Stale message (e.g. canceled, already leased by a duplicate
            # delivery). Drop it cleanly.
            await self._queue.ack(r.config_id, r.reservation_id)
            return False

        worker = self._pick_local_worker(job.config_id)
        if worker is None:
            # No local worker — leave it for the next tick or another node.
            await self._queue.nack(r.config_id, r.reservation_id)
            return False

        lease_id = uuid4()
        expires_at = datetime.now(UTC) + timedelta(seconds=self._lease_ttl_seconds)
        leased = await self._state.acquire_lease(
            job.id, worker.worker_id, lease_id, expires_at
        )
        if leased is None:
            # Lost the CAS race against another dispatcher / sweeper.
            await self._queue.nack(r.config_id, r.reservation_id)
            return False

        try:
            await self._dispatch_ipc(worker, leased, lease_id, expires_at)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ipc dispatch to %s for job %s failed: %s",
                worker.worker_id, job.id, e,
            )
            # Revert lease so another tick / dispatcher can pick it up.
            await self._state.transition(
                job.id,
                expected_from=JobStatus.LEASED,
                to=JobStatus.QUEUED,
                patch={
                    "assigned_worker_id": None,
                    "lease_id": None,
                    "lease_expires_at": None,
                },
            )
            await self._queue.nack(r.config_id, r.reservation_id)
            return False

        await self._queue.ack(r.config_id, r.reservation_id)
        log.info(
            "dispatched job_id=%s to worker_id=%s (lease=%s)",
            job.id, worker.worker_id, lease_id,
        )
        return True

    def _pick_local_worker(self, config_id: str | None) -> ChildProcess | None:
        if not config_id:
            return None
        for c in self._children:
            if c.config_id == config_id and c.is_alive():
                return c
        return None

    async def _dispatch_ipc(
        self,
        worker: ChildProcess,
        job: Job,
        lease_id: UUID,
        lease_expires_at: datetime,
    ) -> None:
        msg: dict[str, Any] = {
            "op": "run",
            "job": job.model_dump(mode="json"),
            "lease_id": str(lease_id),
            "lease_expires_at": lease_expires_at.isoformat(),
        }
        payload = json.dumps(msg).encode("utf-8")

        reader, writer = await asyncio.open_unix_connection(str(worker.ipc_socket))
        try:
            writer.write(len(payload).to_bytes(4, "big"))
            writer.write(payload)
            await writer.drain()

            length_bytes = await reader.readexactly(4)
            length = int.from_bytes(length_bytes, "big")
            body = await reader.readexactly(length)
            reply = json.loads(body)

            if not reply.get("ok"):
                raise RuntimeError(
                    f"worker rejected dispatch: {reply.get('error', 'unknown')}"
                )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
