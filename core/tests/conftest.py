"""Shared test fixtures.

These tests require a running MongoDB. By default they target
`mongodb://localhost:27017` (override via SPINDLE_TEST_MONGO_URL). Each test
gets its own database (named `spindle_test_<random>`), which is dropped in
teardown — no cross-test pollution.

Local dev: `brew services start mongodb-community@7.0`
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from spindle_core.state.mongo import MongoStateStore

_MONGO_URL = os.environ.get("SPINDLE_TEST_MONGO_URL", "mongodb://localhost:27017")


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
