"""Minimal `/health` HTTP endpoint for the Docker healthcheck (SPEC §4.7).

Hand-rolled on `asyncio.start_server` rather than pulling in a web
framework: SPEC §2 only lists python-telegram-bot/httpx/anthropic/
pydantic-settings as deps, and the only thing needed here is
"GET /health -> 200 {status: ok}".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_OK_BODY = b'{"status": "ok"}'
_DOWN_BODY = b'{"status": "down"}'


async def _handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, is_alive: Callable[[], bool]
) -> None:
    try:
        await asyncio.wait_for(reader.readline(), timeout=5)  # request line; path is irrelevant
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not line or line in (b"\r\n", b"\n"):
                break
        alive = is_alive()
        status = "200 OK" if alive else "503 Service Unavailable"
        body = _OK_BODY if alive else _DOWN_BODY
        response = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        writer.write(response)
        await writer.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


async def run_health_server(is_alive: Callable[[], bool], port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(r, w, is_alive), host="0.0.0.0", port=port
    )
    logger.info("Health endpoint listening on :%d/health", port)
    async with server:
        await server.serve_forever()
