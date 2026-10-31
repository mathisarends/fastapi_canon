"""Run an ASGI application on a real local port for engine tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` with uvicorn and yield its ``ws://`` base URL."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
