"""Redis Streams-backed JobQueue.

Per-config stream key: `{prefix}:{config_id}` (e.g., `spindle:queue:qwen-text-v1`).
Single consumer group per stream, named by `consumer_group` (e.g. "dispatchers").

Message body fields:
    job_id     — UUID as 36-char string (Redis fields are bytes)
    priority   — int as decimal string

Delivery model: at-least-once. Reservation IDs are Redis stream message IDs
(strings like "1715698123456-0"). nack(requeue=True) re-XADDs a fresh copy and
ACKs the original — Redis Streams doesn't have a native NACK that re-queues.
"""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from spindle_core.queue.protocol import Reserved


class RedisStreamsQueue:
    def __init__(
        self,
        client: Redis,
        *,
        prefix: str = "spindle:queue",
        consumer_group: str = "dispatchers",
    ) -> None:
        self._r = client
        self._prefix = prefix
        self._group = consumer_group
        self._groups_created: set[str] = set()

    def _stream(self, config_id: str) -> str:
        return f"{self._prefix}:{config_id}"

    async def ensure_group(self, config_id: str) -> None:
        if config_id in self._groups_created:
            return
        stream = self._stream(config_id)
        try:
            # MKSTREAM creates the stream if it doesn't exist; "$" means start
            # consuming from the latest, ignoring history.
            await self._r.xgroup_create(stream, self._group, id="$", mkstream=True)
        except ResponseError as e:
            # BUSYGROUP means the group already exists — fine, idempotent.
            if "BUSYGROUP" not in str(e):
                raise
        self._groups_created.add(config_id)

    async def enqueue(
        self,
        config_id: str,
        job_id: UUID,
        *,
        priority: int = 5,
    ) -> None:
        await self.ensure_group(config_id)
        # redis-py's xadd `fields` param is typed Dict[FieldT, EncodableT] —
        # invariance bites us even though str/str is in the accepted union.
        await self._r.xadd(  # pyright: ignore[reportArgumentType]
            self._stream(config_id),
            {"job_id": str(job_id), "priority": str(priority)},
        )

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
        for cid in config_ids:
            await self.ensure_group(cid)

        # XREADGROUP supports multi-stream reads in one round trip.
        # Per the protocol spec: streams expects a dict of stream → start_id.
        # ">" means "messages never delivered to other consumers in this group."
        streams = {self._stream(cid): ">" for cid in config_ids}
        result = await self._r.xreadgroup(
            groupname=self._group,
            consumername=consumer,
            streams=streams,  # pyright: ignore[reportArgumentType]
            count=count,
            block=block_ms,
        )
        if not result:
            return []

        reserved: list[Reserved] = []
        for stream_key, messages in result:
            stream_name = _decode(stream_key)
            config_id = stream_name[len(self._prefix) + 1 :]  # strip "{prefix}:"
            for msg_id, fields in messages:
                reserved.append(
                    Reserved(
                        job_id=UUID(_decode(fields[b"job_id"])),
                        config_id=config_id,
                        reservation_id=_decode(msg_id),
                        priority=int(_decode(fields.get(b"priority", b"5"))),
                        delivery_count=1,
                    )
                )
        return reserved

    async def ack(self, config_id: str, reservation_id: str) -> None:
        await self._r.xack(self._stream(config_id), self._group, reservation_id)

    async def nack(
        self,
        config_id: str,
        reservation_id: str,
        *,
        requeue: bool = True,
    ) -> None:
        stream = self._stream(config_id)
        if requeue:
            # Read the original message body, re-XADD it, then ACK the original.
            # XRANGE with single-id bounds returns the message we want.
            entries = await self._r.xrange(stream, min=reservation_id, max=reservation_id)
            if entries:
                _, fields = entries[0]
                # Decode bytes → str for re-add
                body = {_decode(k): _decode(v) for k, v in fields.items()}
                await self._r.xadd(stream, body)  # pyright: ignore[reportArgumentType]
        await self._r.xack(stream, self._group, reservation_id)

    async def depth(self, config_id: str) -> int:
        # XLEN counts messages in the stream — includes acked-but-not-trimmed.
        # XPENDING gives us the unacked count. For "approximate depth" we use
        # XLEN minus whatever's already been acked. Simpler: rely on XLEN as a
        # ceiling; close enough for backpressure decisions.
        return int(await self._r.xlen(self._stream(config_id)))

    async def reclaim_stale(
        self,
        config_id: str,
        consumer: str,
        *,
        idle_ms: int,
    ) -> list[Reserved]:
        await self.ensure_group(config_id)
        stream = self._stream(config_id)

        # XAUTOCLAIM: hand off pending messages older than idle_ms to `consumer`.
        # Returns (next_start_id, claimed_messages, deleted_ids). We ignore the
        # deleted list; claim is what we want.
        result = await self._r.xautoclaim(
            name=stream,
            groupname=self._group,
            consumername=consumer,
            min_idle_time=idle_ms,
            start_id="0-0",
        )
        # redis-py returns a 3-tuple (next_id, messages, deleted) on Redis ≥7.0.
        if len(result) == 3:
            _next, messages, _deleted = result
        else:
            _next, messages = result  # older redis-py shape

        reserved: list[Reserved] = []
        for msg_id, fields in messages:
            if not fields:
                # Message was deleted out from under the group; skip.
                continue
            reserved.append(
                Reserved(
                    job_id=UUID(_decode(fields[b"job_id"])),
                    config_id=config_id,
                    reservation_id=_decode(msg_id),
                    priority=int(_decode(fields.get(b"priority", b"5"))),
                    delivery_count=2,  # at minimum, we know this is a redelivery
                )
            )
        return reserved


def _decode(b: bytes | str) -> str:
    return b.decode() if isinstance(b, bytes) else b
