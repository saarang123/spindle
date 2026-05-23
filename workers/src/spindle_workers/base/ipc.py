"""Unix-socket IPC server for receiving job dispatches from the runtime.

Wire protocol: 4-byte big-endian length prefix + JSON payload.

Operations:
  - ``run``    — accept a job (returns OK immediately, runs async). Rejected
                 with ``AT_CAPACITY`` if ``concurrency_used >= concurrency_limit``.
  - ``cancel`` — trip the cancel token for a running job_id.
  - ``ping``   — health check.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("spindle_workers")


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class IpcServer:
    """Single-listener Unix-socket JSON-RPC server.

    Dispatches messages to ``handler(msg) -> reply``. The reply is sent back
    on the same connection and the socket is closed.
    """

    def __init__(self, socket_path: str | Path, handler: Handler) -> None:
        self.socket_path = Path(socket_path)
        self._handler = handler
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        """Bind and listen. Removes any stale socket file first."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Clean up a stale socket from a previous run if present.
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self.socket_path),
        )
        log.info("ipc server listening at %s", self.socket_path)

    async def serve_forever(self) -> None:
        """Block until cancelled."""
        if self._server is None:
            raise RuntimeError("call start() first")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Close the listener and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            msg = await _read_message(reader)
            log.debug("ipc recv: %s", msg.get("op"))
            reply = await self._handler(msg)
        except _ProtocolError as e:
            reply = {"ok": False, "error": f"PROTOCOL: {e}"}
        except Exception as e:  # noqa: BLE001 — defensive at the wire boundary
            log.exception("ipc handler failed")
            reply = {"ok": False, "error": f"INTERNAL: {e}"}
        try:
            await _write_message(writer, reply)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ─── wire helpers ───────────────────────────────────────────────────


class _ProtocolError(Exception):
    pass


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        length_bytes = await reader.readexactly(4)
    except asyncio.IncompleteReadError as e:
        raise _ProtocolError(f"short length prefix: {len(e.partial)} bytes") from e
    length = int.from_bytes(length_bytes, "big")
    if length <= 0 or length > 16 * 1024 * 1024:
        raise _ProtocolError(f"invalid length: {length}")
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as e:
        raise _ProtocolError(
            f"short payload: {len(e.partial)} of {length} bytes"
        ) from e
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise _ProtocolError(f"invalid JSON: {e}") from e


async def _write_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    payload = json.dumps(msg).encode("utf-8")
    writer.write(len(payload).to_bytes(4, "big"))
    writer.write(payload)
    await writer.drain()
