from __future__ import annotations

import asyncio
import contextlib

import pytest

from paperbot.health import run_health_server


async def _get(port: int, path: str = "/health") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    status_line = await reader.readline()
    status_code = int(status_line.split(b" ", 2)[1])
    body = b""
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
    with contextlib.suppress(TimeoutError):
        body = await asyncio.wait_for(reader.read(1024), timeout=0.5)
    writer.close()
    return status_code, body


@pytest.fixture
async def running_server() -> tuple[int, list[bool]]:
    alive_flag = [True]
    port = 18099
    task = asyncio.create_task(run_health_server(lambda: alive_flag[0], port))
    await asyncio.sleep(0.1)
    try:
        yield port, alive_flag
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_health_returns_200_when_alive(running_server: tuple[int, list[bool]]) -> None:
    port, _ = running_server
    status, body = await _get(port)
    assert status == 200
    assert b"ok" in body


async def test_health_returns_503_when_not_alive(running_server: tuple[int, list[bool]]) -> None:
    port, alive_flag = running_server
    alive_flag[0] = False
    status, body = await _get(port)
    assert status == 503
    assert b"down" in body
