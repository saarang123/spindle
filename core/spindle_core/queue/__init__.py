"""Queue backends — implementations of the JobQueue protocol.

Use `make_queue(settings)` to construct the backend selected by
SPINDLE_QUEUE_BACKEND. Backends are imported lazily so missing optional
drivers don't blow up at import time.
"""

from __future__ import annotations

from spindle_core.queue.protocol import JobQueue, Reserved
from spindle_core.settings import Settings

__all__ = ["JobQueue", "Reserved", "make_queue"]


def make_queue(settings: Settings) -> JobQueue:
    match settings.queue_backend:
        case "redis":
            from redis.asyncio import Redis

            from spindle_core.queue.redis_streams import RedisStreamsQueue

            client = Redis.from_url(settings.redis_url)
            return RedisStreamsQueue(
                client,
                prefix=settings.redis_queue_prefix,
                consumer_group=settings.redis_consumer_group,
            )
        case "memory":
            from spindle_core.queue.memory import MemoryJobQueue

            return MemoryJobQueue()
        case other:
            raise ValueError(f"unknown queue backend: {other!r}")
