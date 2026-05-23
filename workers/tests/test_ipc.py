"""IpcServer round-trip tests over a real Unix socket.

macOS caps AF_UNIX paths at ~104 chars, which pytest's tmp_path easily blows
through. Use short paths under /tmp with a hex suffix per test.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from spindle_workers.base.ipc import IpcServer


@pytest.fixture
def sock_path(tmp_path_factory) -> Path:
    # Short path; remove on teardown.
    p = Path(f"/tmp/spindle-ipc-test-{uuid4().hex[:8]}.sock")
    yield p
    try:
        p.unlink()
    except FileNotFoundError:
        pass


async def _send(socket_path: Path, msg: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    payload = json.dumps(msg).encode("utf-8")
    writer.write(len(payload).to_bytes(4, "big"))
    writer.write(payload)
    await writer.drain()
    length_bytes = await reader.readexactly(4)
    length = int.from_bytes(length_bytes, "big")
    body = await reader.readexactly(length)
    writer.close()
    await writer.wait_closed()
    return json.loads(body)


async def test_ping_round_trip(sock_path: Path) -> None:
    async def handler(msg: dict) -> dict:
        return {"ok": True, "echo": msg.get("op")}

    server = IpcServer(sock_path, handler=handler)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        reply = await _send(sock_path, {"op": "ping"})
        assert reply == {"ok": True, "echo": "ping"}
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        await server.stop()


async def test_handler_exception_returns_internal_error(sock_path: Path) -> None:
    async def boom(_msg: dict) -> dict:
        raise RuntimeError("boom")

    server = IpcServer(sock_path, handler=boom)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        reply = await _send(sock_path, {"op": "run"})
        assert reply["ok"] is False
        assert "INTERNAL" in reply["error"]
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        await server.stop()


async def test_protocol_error_on_invalid_length(sock_path: Path) -> None:
    async def handler(_msg: dict) -> dict:
        return {"ok": True}

    server = IpcServer(sock_path, handler=handler)
    await server.start()
    serve_task = asyncio.create_task(server.serve_forever())
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        # Zero-length prefix → protocol error.
        writer.write((0).to_bytes(4, "big"))
        await writer.drain()
        length_bytes = await reader.readexactly(4)
        length = int.from_bytes(length_bytes, "big")
        body = await reader.readexactly(length)
        writer.close()
        await writer.wait_closed()
        reply = json.loads(body)
        assert reply["ok"] is False
        assert "PROTOCOL" in reply["error"]
    finally:
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
        await server.stop()


async def test_stop_removes_socket_file(sock_path: Path) -> None:
    async def handler(_msg: dict) -> dict:
        return {"ok": True}

    server = IpcServer(sock_path, handler=handler)
    await server.start()
    assert sock_path.exists()
    await server.stop()
    assert not sock_path.exists()
