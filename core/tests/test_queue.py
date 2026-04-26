"""JobQueue conformance suite.

Parametrized over backends (memory + redis). Every test runs twice; any
divergence between the impls is a bug in one of them.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from spindle_core.queue.protocol import JobQueue

# ─── conformance — protocol satisfied ────────────────────────────────


def test_satisfies_protocol(job_queue: JobQueue) -> None:
    assert isinstance(job_queue, JobQueue)


# ─── enqueue / reserve round trip ────────────────────────────────────


async def test_enqueue_then_reserve(job_queue: JobQueue) -> None:
    cid = "qwen-text-v1"
    job_id = uuid4()
    await job_queue.enqueue(cid, job_id, priority=7)

    reserved = await job_queue.reserve([cid], consumer="d-0", count=1, block_ms=200)
    assert len(reserved) == 1
    r = reserved[0]
    assert r.job_id == job_id
    assert r.config_id == cid
    assert r.priority == 7
    assert r.delivery_count == 1


async def test_reserve_empty_returns_empty_after_block(job_queue: JobQueue) -> None:
    reserved = await job_queue.reserve(["nothing"], consumer="d-0", count=1, block_ms=50)
    assert reserved == []


async def test_reserve_no_configs_returns_empty(job_queue: JobQueue) -> None:
    assert await job_queue.reserve([], consumer="d-0") == []


async def test_reserve_across_configs(job_queue: JobQueue) -> None:
    a_id = uuid4()
    b_id = uuid4()
    await job_queue.enqueue("config-a", a_id)
    await job_queue.enqueue("config-b", b_id)

    reserved = await job_queue.reserve(
        ["config-a", "config-b"], consumer="d-0", count=10, block_ms=200
    )
    seen = {(r.config_id, r.job_id) for r in reserved}
    assert ("config-a", a_id) in seen
    assert ("config-b", b_id) in seen


# ─── ack / nack ──────────────────────────────────────────────────────


async def test_ack_removes_message(job_queue: JobQueue) -> None:
    cid = "c"
    await job_queue.enqueue(cid, uuid4())
    reserved = await job_queue.reserve([cid], consumer="d-0", block_ms=200)
    assert len(reserved) == 1

    await job_queue.ack(cid, reserved[0].reservation_id)

    # Reserve again — should be empty (no other consumer can take an acked msg).
    again = await job_queue.reserve([cid], consumer="d-1", block_ms=50)
    assert again == []


async def test_nack_with_requeue_makes_available_again(job_queue: JobQueue) -> None:
    cid = "c"
    job_id = uuid4()
    await job_queue.enqueue(cid, job_id)
    first = await job_queue.reserve([cid], consumer="d-0", block_ms=200)
    assert len(first) == 1

    await job_queue.nack(cid, first[0].reservation_id, requeue=True)

    # A different consumer (or even the same) should be able to take it.
    second = await job_queue.reserve([cid], consumer="d-1", block_ms=200)
    assert len(second) == 1
    assert second[0].job_id == job_id


async def test_nack_without_requeue_drops(job_queue: JobQueue) -> None:
    cid = "c"
    await job_queue.enqueue(cid, uuid4())
    reserved = await job_queue.reserve([cid], consumer="d-0", block_ms=200)
    await job_queue.nack(cid, reserved[0].reservation_id, requeue=False)

    again = await job_queue.reserve([cid], consumer="d-1", block_ms=50)
    assert again == []


# ─── depth ───────────────────────────────────────────────────────────


async def test_depth_reflects_enqueues(job_queue: JobQueue) -> None:
    cid = "c"
    await job_queue.ensure_group(cid)
    assert await job_queue.depth(cid) == 0
    for _ in range(3):
        await job_queue.enqueue(cid, uuid4())
    assert await job_queue.depth(cid) == 3


# ─── reclaim_stale ───────────────────────────────────────────────────


async def test_reclaim_stale_picks_up_abandoned(job_queue: JobQueue) -> None:
    cid = "c"
    job_id = uuid4()
    await job_queue.enqueue(cid, job_id)

    # Consumer reserves but never acks.
    reserved = await job_queue.reserve([cid], consumer="dead-d", block_ms=200)
    assert len(reserved) == 1

    # Wait long enough that idle_ms threshold is crossed.
    await asyncio.sleep(0.05)

    reclaimed = await job_queue.reclaim_stale(cid, consumer="alive-d", idle_ms=20)
    assert len(reclaimed) == 1
    assert reclaimed[0].job_id == job_id
    # delivery_count should reflect the redelivery (>= 2)
    assert reclaimed[0].delivery_count >= 2


async def test_reclaim_stale_does_not_pick_up_fresh(job_queue: JobQueue) -> None:
    cid = "c"
    await job_queue.enqueue(cid, uuid4())
    reserved = await job_queue.reserve([cid], consumer="d-0", block_ms=200)
    assert len(reserved) == 1

    # No wait — message is fresh.
    reclaimed = await job_queue.reclaim_stale(cid, consumer="d-1", idle_ms=10_000)
    assert reclaimed == []


# ─── ensure_group is idempotent ──────────────────────────────────────


async def test_ensure_group_idempotent(job_queue: JobQueue) -> None:
    cid = "c"
    await job_queue.ensure_group(cid)
    await job_queue.ensure_group(cid)
    await job_queue.ensure_group(cid)
    # No exception. State is functional.
    await job_queue.enqueue(cid, uuid4())
    reserved = await job_queue.reserve([cid], consumer="d", block_ms=200)
    assert len(reserved) == 1
