"""JobQueue protocol — the contract every queue backend implements.

A JobQueue carries pointers (config_id + job_id), not job bodies. The full Job
state lives in StateStore. Decoupling means queue messages stay tiny and we
can re-derive everything from state if a queue is wiped.

Semantics (every backend MUST satisfy):
  - At-least-once delivery. The dispatcher and workers must be idempotent.
  - reserve() blocks up to `block_ms`; returns [] on timeout.
  - ack() removes a reserved message permanently.
  - nack(requeue=True) makes the message available again immediately for
    another consumer. nack(requeue=False) is ack-equivalent (drop without
    success).
  - reclaim_stale() picks up messages that were reserved but never acked
    after `idle_ms` — used for worker / dispatcher crash recovery.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Reserved(BaseModel):
    """A reservation handed back from `reserve()`. Pass back to ack/nack
    along with config_id."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    config_id: str
    reservation_id: str  # backend-opaque (Redis stream ID, etc.)
    priority: int = 5
    delivery_count: int = 1  # 1 on first delivery, increments on reclaim


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self,
        config_id: str,
        job_id: UUID,
        *,
        priority: int = 5,
    ) -> None:
        """Append a pointer to the config's stream. Idempotency is the
        caller's responsibility — the queue does not dedupe."""
        ...

    async def reserve(
        self,
        config_ids: list[str],
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 1000,
    ) -> list[Reserved]:
        """Reserve up to `count` messages from the union of `config_ids`'
        streams. Blocks up to `block_ms` if all are empty. Returns [] on
        timeout. The same `consumer` name should be used by a stable
        identity (e.g., dispatcher name) so reclaim_stale can find its
        own stale work."""
        ...

    async def ack(self, config_id: str, reservation_id: str) -> None:
        """Acknowledge — message is gone."""
        ...

    async def nack(
        self,
        config_id: str,
        reservation_id: str,
        *,
        requeue: bool = True,
    ) -> None:
        """If requeue, make the message available again immediately.
        If not, drop without success."""
        ...

    async def depth(self, config_id: str) -> int:
        """Approximate depth: messages produced minus messages acked.
        Includes pending (reserved-but-not-acked) messages."""
        ...

    async def reclaim_stale(
        self,
        config_id: str,
        consumer: str,
        *,
        idle_ms: int,
    ) -> list[Reserved]:
        """Reassign messages that have been reserved (but not acked) longer
        than `idle_ms` to this `consumer`. Returns the reclaimed reservations."""
        ...

    async def ensure_group(self, config_id: str) -> None:
        """Idempotent. Create the stream + consumer group if missing.
        Call before any reserve() against a config_id."""
        ...
