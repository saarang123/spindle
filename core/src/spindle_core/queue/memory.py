"""In-process MemoryJobQueue — for tests and single-process dev.

Mirrors Redis Streams semantics:
  - At-least-once delivery
  - Per-config FIFO
  - Reservation lifecycle (reserved → ack | nack | reclaim)
  - Stale reclaim by idle time

Not safe across processes. Not durable.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from spindle_core.queue.protocol import Reserved


@dataclass
class _Pending:
    """A message currently reserved by a consumer but not yet acked."""

    reservation_id: str
    job_id: UUID
    config_id: str
    priority: int
    delivery_count: int
    consumer: str
    reserved_at_ms: float


@dataclass
class _Stream:
    available: deque[_Pending] = field(default_factory=deque)
    pending: dict[str, _Pending] = field(default_factory=dict)
    total_enqueued: int = 0


def _now_ms() -> float:
    return time.monotonic() * 1000


class MemoryJobQueue:
    def __init__(self) -> None:
        self._streams: dict[str, _Stream] = defaultdict(_Stream)
        # One condition per stream so reserve() can wait when empty.
        self._not_empty: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def ensure_group(self, config_id: str) -> None:
        # Stream is auto-created on first access via defaultdict; no-op here.
        _ = self._streams[config_id]

    async def enqueue(
        self,
        config_id: str,
        job_id: UUID,
        *,
        priority: int = 5,
    ) -> None:
        stream = self._streams[config_id]
        stream.total_enqueued += 1
        msg = _Pending(
            reservation_id=str(uuid4()),
            job_id=job_id,
            config_id=config_id,
            priority=priority,
            delivery_count=1,
            consumer="",
            reserved_at_ms=0.0,
        )
        stream.available.append(msg)
        async with self._not_empty[config_id]:
            self._not_empty[config_id].notify_all()

    async def reserve(
        self,
        config_ids: list[str],
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 1000,
    ) -> list[Reserved]:
        if not config_ids:
            return []

        deadline = _now_ms() + block_ms
        while True:
            reserved = self._take_available(config_ids, consumer, count)
            if reserved:
                return reserved

            remaining_ms = deadline - _now_ms()
            if remaining_ms <= 0:
                return []

            # Wait on the union of streams becoming non-empty.
            # Simple approach: poll with short sleep; for in-memory testing
            # this is fine. (A "real" multi-stream wait would use a shared
            # condition, but at test scale 25ms granularity is invisible.)
            await asyncio.sleep(min(remaining_ms / 1000, 0.025))

    def _take_available(self, config_ids: list[str], consumer: str, count: int) -> list[Reserved]:
        out: list[Reserved] = []
        now = _now_ms()
        for cid in config_ids:
            stream = self._streams[cid]
            while stream.available and len(out) < count:
                msg = stream.available.popleft()
                msg.consumer = consumer
                msg.reserved_at_ms = now
                stream.pending[msg.reservation_id] = msg
                out.append(
                    Reserved(
                        job_id=msg.job_id,
                        config_id=msg.config_id,
                        reservation_id=msg.reservation_id,
                        priority=msg.priority,
                        delivery_count=msg.delivery_count,
                    )
                )
            if len(out) >= count:
                break
        return out

    async def ack(self, config_id: str, reservation_id: str) -> None:
        self._streams[config_id].pending.pop(reservation_id, None)

    async def nack(
        self,
        config_id: str,
        reservation_id: str,
        *,
        requeue: bool = True,
    ) -> None:
        stream = self._streams[config_id]
        msg = stream.pending.pop(reservation_id, None)
        if msg is None:
            return
        if requeue:
            # New reservation_id on requeue — Redis Streams gives a new ID too.
            msg.reservation_id = str(uuid4())
            msg.delivery_count += 1
            msg.consumer = ""
            msg.reserved_at_ms = 0.0
            stream.available.append(msg)
            async with self._not_empty[config_id]:
                self._not_empty[config_id].notify_all()

    async def depth(self, config_id: str) -> int:
        stream = self._streams[config_id]
        return len(stream.available) + len(stream.pending)

    async def reclaim_stale(
        self,
        config_id: str,
        consumer: str,
        *,
        idle_ms: int,
    ) -> list[Reserved]:
        stream = self._streams[config_id]
        now = _now_ms()
        to_reclaim = [
            msg for msg in stream.pending.values() if (now - msg.reserved_at_ms) >= idle_ms
        ]
        out: list[Reserved] = []
        for msg in to_reclaim:
            msg.consumer = consumer
            msg.reserved_at_ms = now
            msg.delivery_count += 1
            out.append(
                Reserved(
                    job_id=msg.job_id,
                    config_id=msg.config_id,
                    reservation_id=msg.reservation_id,
                    priority=msg.priority,
                    delivery_count=msg.delivery_count,
                )
            )
        return out
