"""A raw ``fastapi-webrtc.v1`` signaling client for runtime tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any

import websockets
from fastapi_canon.integrations.webrtc import SUBPROTOCOL
from websockets.asyncio.client import ClientConnection


class Signal:
    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.received: list[dict[str, Any]] = []
        self.hello: dict[str, Any] = {}

    @classmethod
    async def open(
        cls,
        url: str,
        *,
        subprotocols: Sequence[str] = (SUBPROTOCOL,),
        origin: str | None = None,
    ) -> Signal:
        offered = [websockets.Subprotocol(p) for p in subprotocols]
        ws = await websockets.connect(
            url,
            subprotocols=offered or None,
            origin=None if origin is None else websockets.Origin(origin),
        )
        return cls(ws)

    @classmethod
    async def start(cls, url: str, *, origin: str | None = None) -> Signal:
        """Connect and read the session hello."""
        signal = await cls.open(url, origin=origin)
        signal.hello = await signal.expect("session")
        return signal

    async def recv(self, timeout: float = 5) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            raw = await self.ws.recv()
        message: dict[str, Any] = json.loads(raw)
        self.received.append(message)
        return message

    async def expect(self, kind: str, timeout: float = 5) -> dict[str, Any]:
        """Read until a message of ``kind`` arrives."""
        async with asyncio.timeout(timeout):
            while True:
                message = await self.recv(timeout)
                if message["type"] == kind:
                    return message

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["type"] == kind]

    async def send(self, payload: dict[str, Any] | str | bytes) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        await self.ws.send(payload)

    async def describe(self, kind: str, sdp: str, negotiation: int = 1) -> None:
        await self.send(
            {
                "type": "description",
                "description": {"type": kind, "sdp": sdp},
                "negotiation": negotiation,
            }
        )

    async def candidate(
        self, candidate: str | None, ufrag: str | None = "remote"
    ) -> None:
        payload: dict[str, Any] | None = None
        if candidate is not None:
            payload = {"candidate": candidate, "sdpMid": "0", "sdpMLineIndex": 0}
            if ufrag is not None:
                payload["usernameFragment"] = ufrag
        await self.send({"type": "candidate", "candidate": payload})

    async def closed(self, timeout: float = 5) -> tuple[int | None, str | None]:
        """Read remaining messages until the server closes the WebSocket."""
        async with asyncio.timeout(timeout):
            try:
                while True:
                    await self.recv(timeout)
            except websockets.ConnectionClosed:
                pass
        return self.ws.close_code, self.ws.close_reason

    async def close(self, code: int = 1000) -> None:
        await self.ws.close(code=code)


async def until(condition: Callable[[], object], timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
