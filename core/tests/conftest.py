"""Shared test fixtures.

State tests require a running MongoDB. Queue tests run against memory and
(if available) a real Redis. Artifact tests run against memory and a real
MinIO/S3 (via SPINDLE_S3_*).

Defaults assume local services on the control node:
    brew services start mongodb-community@7.0   # macOS
    brew services start redis
    docker compose -f infra/minio/compose.yaml up -d   # MinIO on localhost

Override via SPINDLE_TEST_MONGO_URL / SPINDLE_TEST_REDIS_URL / SPINDLE_S3_*.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from redis.asyncio import Redis

# Load .env at the repo root so SPINDLE_S3_* etc. are visible to tests.
# Existing env vars win (so CI / local overrides aren't trampled).
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

from spindle_core.artifacts.memory import MemoryArtifactStore  # noqa: E402
from spindle_core.artifacts.protocol import ArtifactStore  # noqa: E402
from spindle_core.artifacts.s3 import S3ArtifactStore  # noqa: E402
from spindle_core.queue.memory import MemoryJobQueue  # noqa: E402
from spindle_core.queue.protocol import JobQueue  # noqa: E402
from spindle_core.queue.redis_streams import RedisStreamsQueue  # noqa: E402
from spindle_core.state.mongo import MongoStateStore  # noqa: E402

_MONGO_URL = os.environ.get("SPINDLE_TEST_MONGO_URL", "mongodb://localhost:27017")
_REDIS_URL = os.environ.get("SPINDLE_TEST_REDIS_URL", "redis://localhost:6379/15")
_S3_ENDPOINT = os.environ.get("SPINDLE_S3_ENDPOINT", "http://localhost:9000")
_S3_BUCKET = os.environ.get("SPINDLE_S3_BUCKET", "spindle-artifacts")
_S3_ACCESS_KEY = os.environ.get("SPINDLE_S3_ACCESS_KEY", "spindle")
_S3_SECRET_KEY = os.environ.get("SPINDLE_S3_SECRET_KEY", "")
_S3_REGION = os.environ.get("SPINDLE_S3_REGION", "us-east-1")


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


# ─── artifact store ─────────────────────────────────────────────────


@pytest_asyncio.fixture(params=["memory", "s3"])
async def artifact_store(request) -> AsyncIterator[ArtifactStore]:  # type: ignore[no-untyped-def]
    backend = request.param
    if backend == "memory":
        yield MemoryArtifactStore()
        return

    # S3 via the live MinIO. Each test gets a unique key prefix; teardown
    # deletes only those keys (cheap; doesn't touch other tests' data).
    if not _S3_SECRET_KEY:
        import pytest

        pytest.skip("SPINDLE_S3_SECRET_KEY unset — skipping live S3 tests")

    store = S3ArtifactStore(
        endpoint=_S3_ENDPOINT,
        bucket=_S3_BUCKET,
        access_key=_S3_ACCESS_KEY,
        secret_key=_S3_SECRET_KEY,
        region=_S3_REGION,
    )
    prefix = f"_test/{uuid4().hex[:12]}/"
    # Stash on the instance so tests can scope their keys.
    store._test_prefix = prefix  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        # Best-effort cleanup: list and delete everything under our prefix.
        try:
            async with store._client() as s3:
                paginator = s3.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=store._bucket, Prefix=prefix):
                    contents = page.get("Contents") or []
                    if not contents:
                        continue
                    await s3.delete_objects(
                        Bucket=store._bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
                    )
        except Exception:  # pragma: no cover — teardown should never fail tests
            pass
