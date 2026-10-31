"""A minimal aiortc peer that speaks ``fastapi-webrtc.v1`` like the browser client.

Only tests use it: it drives real engine sessions over a real WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence
from typing import Any

import websockets
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCDataChannel,
    RTCPeerConnection,
    RTCSessionDescription,
)
from fastapi_canon.integrations.webrtc import SUBPROTOCOL
from websockets.asyncio.client import ClientConnection
from websockets.typing import Subprotocol


class PeerClient:
    def __init__(self, url: str, *, tracks: Sequence[MediaStreamTrack] = ()) -> None:
        self.url = url
        self.tracks = tuple(tracks)
        self.hello: dict[str, Any] = {}
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channels: dict[str, RTCDataChannel] = {}
        self.messages: dict[str, asyncio.Queue[str | bytes]] = {}
        self.remote_tracks: asyncio.Queue[MediaStreamTrack] = asyncio.Queue()
        self.errors: list[dict[str, Any]] = []
        self.ready = asyncio.Event()
        self.renegotiations = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._closed = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._negotiation = 0
        self._channel_open: dict[str, asyncio.Event] = {}
        self.pc.on("track", self.remote_tracks.put_nowait)
        self.pc.on("datachannel", self._bind)

    async def connect(self, *, timeout: float = 15) -> PeerClient:
        async with asyncio.timeout(timeout):
            self._ws = await websockets.connect(
                self.url, subprotocols=[Subprotocol(SUBPROTOCOL)], max_size=2**22
            )
            first = await self._ws.recv()
            self.hello = json.loads(first)
            assert self.hello["type"] == "session", self.hello
            offerer = self.hello["role"] == "answerer"
            for channel in self.hello["channels"]:
                name = channel["name"]
                self._channel_open[name] = asyncio.Event()
                self.messages[name] = asyncio.Queue()
                if channel["negotiated"]:
                    self._bind(
                        self.pc.createDataChannel(
                            name,
                            negotiated=True,
                            id=channel["id"],
                            ordered=channel["ordered"],
                            maxRetransmits=channel["max_retransmits"],
                            maxPacketLifeTime=channel["max_packet_life_time_ms"],
                        )
                    )
                elif offerer:
                    self._bind(
                        self.pc.createDataChannel(name, ordered=channel["ordered"])
                    )
            for track in self.tracks:
                self.pc.addTrack(track)
            if offerer:
                sent = {track.kind for track in self.tracks}
                for kind in ("audio", "video"):
                    direction = self.hello["media"][kind]
                    if direction in ("send", "send_receive") and kind not in sent:
                        self.pc.addTransceiver(kind, direction="recvonly")
            self._reader = asyncio.create_task(self._read())
            if offerer:
                await self._offer()
            await self.ready.wait()
            for event in self._channel_open.values():
                await event.wait()
        return self

    async def send_raw(self, payload: dict[str, Any] | str) -> None:
        assert self._ws is not None
        text = payload if isinstance(payload, str) else json.dumps(payload)
        await self._ws.send(text)

    def send(self, channel: str, message: str | bytes) -> None:
        self.channels[channel].send(message)

    def send_json(self, channel: str, message: Any) -> None:
        self.channels[channel].send(json.dumps(message))

    async def receive(self, channel: str, timeout: float = 10) -> str | bytes:
        async with asyncio.timeout(timeout):
            return await self.messages[channel].get()

    async def receive_json(self, channel: str, timeout: float = 10) -> Any:
        data = await self.receive(channel, timeout)
        assert isinstance(data, str)
        return json.loads(data)

    async def renegotiate(self) -> None:
        await self._offer()

    async def wait_closed(self, timeout: float = 10) -> tuple[int | None, str | None]:
        async with asyncio.timeout(timeout):
            await self._closed.wait()
        return self.close_code, self.close_reason

    async def close(self, code: int = 1000) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close(code=code)
        if self._reader is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._reader, 5)
        await self.pc.close()

    async def __aenter__(self) -> PeerClient:
        return await self.connect()

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _offer(self) -> None:
        self._negotiation += 1
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        local = self.pc.localDescription
        await self.send_raw(
            {
                "type": "description",
                "description": {"type": local.type, "sdp": local.sdp},
                "negotiation": self._negotiation,
            }
        )
        await self.send_raw({"type": "candidate", "candidate": None})

    def _bind(self, channel: RTCDataChannel) -> None:
        name = channel.label
        self.channels[name] = channel
        opened = self._channel_open.setdefault(name, asyncio.Event())
        queue = self.messages.setdefault(name, asyncio.Queue())
        channel.on("message", queue.put_nowait)
        channel.on("open", opened.set)
        if channel.readyState == "open":
            opened.set()

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                kind = message["type"]
                if kind == "ready":
                    self.ready.set()
                elif kind == "error":
                    self.errors.append(message)
                elif kind == "candidate":
                    if message["candidate"] is None:
                        await self.pc.addIceCandidate(None)
                elif kind == "description":
                    description = message["description"]
                    await self.pc.setRemoteDescription(
                        RTCSessionDescription(
                            sdp=description["sdp"], type=description["type"]
                        )
                    )
                    if description["type"] == "offer":
                        self.renegotiations += 1
                        answer = await self.pc.createAnswer()
                        await self.pc.setLocalDescription(answer)
                        local = self.pc.localDescription
                        await self.send_raw(
                            {
                                "type": "description",
                                "description": {"type": local.type, "sdp": local.sdp},
                                "negotiation": message["negotiation"],
                            }
                        )
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_code = self._ws.close_code
            self.close_reason = self._ws.close_reason
            self._closed.set()
            self.ready.set()
            for event in self._channel_open.values():
                event.set()
