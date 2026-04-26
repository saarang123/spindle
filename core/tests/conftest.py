"""Shared test fixtures.

State tests require a running MongoDB. Queue tests run against memory and
(if available) a real Redis. Both default to localhost; override via
SPINDLE_TEST_MONGO_URL / SPINDLE_TEST_REDIS_URL.

Local dev:
    brew services start mongodb-community@7.0
    brew services start redis
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

from spindle_core.queue.memory import MemoryJobQueue
from spindle_core.queue.protocol import JobQueue
from spindle_core.queue.redis_streams import RedisStreamsQueue
from spindle_core.state.mongo import MongoStateStore

_MONGO_URL = os.environ.get("SPINDLE_TEST_MONGO_URL", "mongodb://localhost:27017")
_REDIS_URL = os.environ.get("SPINDLE_TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest_asyncio.fixture
async def state_store() -> AsyncIterator[MongoStateStore]:
    db_name = f"spindle_test_{uuid4().hex[:12]}"
    client = AsyncIOMotorClient(_MONGO_URL, uuidRepresentation="standard", tz_aware=True)
    store = MongoStateStore(url=_MONGO_URL, db_name=db_name, client=client)
    await store.ensure_indexes()
    try:
        yield store
    finally:
        await client.drop_database(db_name)
        client.close()


# ─── queue ──────────────────────────────────────────────────────────
#
# Parametrized over both backends. Each test runs twice (memory + redis)
# and any divergence between them is a bug in one impl.


@pytest_asyncio.fixture(params=["memory", "redis"])
async def job_queue(request) -> AsyncIterator[JobQueue]:  # type: ignore[no-untyped-def]
    backend = request.param
    if backend == "memory":
        yield MemoryJobQueue()
        return

    # Redis: scope each test to a unique key prefix + consumer group so
    # parallel tests don't collide. FLUSHDB the test DB on teardown.
    suffix = uuid4().hex[:8]
    client = Redis.from_url(_REDIS_URL)
    queue = RedisStreamsQueue(
        client,
        prefix=f"spindle_test:{suffix}:queue",
        consumer_group=f"test-group-{suffix}",
    )
    try:
        yield queue
    finally:
        # Wipe just the keys we created (don't FLUSHDB — other tests may share).
        async for key in client.scan_iter(f"spindle_test:{suffix}:*"):
            await client.delete(key)
        await client.aclose()
