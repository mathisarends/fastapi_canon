"""Exercise Canon composition and Dishka scopes through real WebSocket sessions."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCDataChannel,
    RTCPeerConnection,
    RTCSessionDescription,
)
from dishka import AsyncContainer, FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi_canon import Composition, Feature
from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Direction,
    ErrorCode,
    MediaPolicy,
    Peer,
    Session,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine
from starlette.testclient import WebSocketDenialResponse


@dataclass
class Resource:
    track: AudioStreamTrack = field(default_factory=AudioStreamTrack)
    closed: bool = False
    close_code: ErrorCode | None = None
    work: list["Work"] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


@dataclass
class Work:
    resource: Resource
    closed: bool = False

    def reply(self, message: str) -> str:
        assert not self.closed and not self.resource.closed
        return message.upper()


class Resources(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Resource] = []

    @provide(scope=Scope.SESSION)
    async def resource(self) -> AsyncIterator[Resource]:
        resource = Resource()
        self.sessions.append(resource)
        yield resource
        resource.events.append(f"dependency_close:{resource.track.readyState}")
        resource.closed = True

    @provide(scope=Scope.REQUEST)
    async def work(self, resource: Resource) -> AsyncIterator[Work]:
        work = Work(resource)
        resource.work.append(work)
        yield work
        work.closed = True


def application() -> tuple[FastAPI, WebRtcRouter, Resources]:
    resources = Resources()
    rtc = WebRtcRouter(engine=AiortcEngine(), max_sessions=1)

    def authorize(token: str = "ok") -> None:
        if token != "ok":
            raise HTTPException(status_code=403)

    @rtc.session(
        "/rtc",
        media=MediaPolicy(audio=Direction.SEND),
        data_channels=[DataChannelSpec(name="chat", receive_model=str, send_model=str)],
        dependencies=[Depends(authorize)],
    )
    @inject
    async def factory(
        resource: FromDishka[Resource], container: FromDishka[AsyncContainer]
    ) -> Session:
        async def on_message(peer: Peer, message: str) -> None:
            async with container(scope=Scope.REQUEST) as request:
                work = await request.get(Work)
                await peer.send_message("chat", work.reply(message))

        async def on_close(peer: Peer, code: ErrorCode) -> None:
            assert not resource.closed
            resource.close_code = code
            resource.events.append(f"on_close:{resource.track.readyState}")

        return Session(
            tracks=[resource.track], on_message={"chat": on_message}, on_close=on_close
        )

    app = Composition(Feature(name="rtc", routers=[rtc], providers=[resources])).apply(
        FastAPI()
    )
    return app, rtc, resources


def test_admission_denial_does_not_allocate_session_dependencies() -> None:
    app, rtc, resources = application()
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as error,
            client.websocket_connect("/rtc?token=denied"),
        ):
            pytest.fail("denied connection was accepted")
        assert error.value.status_code == 403
        assert resources.sessions == []
        assert rtc.active_sessions == 0


def test_capacity_rejection_and_disconnect_preserve_scope_until_cleanup() -> None:
    app, rtc, resources = application()
    with TestClient(app) as client:
        with client.websocket_connect("/rtc") as websocket:
            assert websocket.receive_json()["type"] == "session"
            with client.websocket_connect("/rtc") as rejected:
                assert rejected.receive()["reason"] == "session_limit"
            accepted, refused = resources.sessions
            assert refused.closed and refused.track.readyState == "ended"
            assert refused.close_code is None
            assert refused.events == ["dependency_close:ended"]
            assert not accepted.closed and accepted.track.readyState == "live"
        assert accepted.closed
        assert accepted.close_code is ErrorCode.CLOSED
        assert accepted.events == ["on_close:live", "dependency_close:ended"]
        assert rtc.active_sessions == 0


@pytest.mark.asyncio
async def test_composition_shutdown_closes_sessions_before_dependencies() -> None:
    app, rtc, resources = application()
    inbound: asyncio.Queue = asyncio.Queue()
    outbound: asyncio.Queue = asyncio.Queue()
    inbound.put_nowait({"type": "websocket.connect"})
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/rtc",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
        "state": {},
    }
    async with asyncio.timeout(5):
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope, inbound.get, outbound.put))
            assert (await outbound.get())["type"] == "websocket.accept"
            assert json.loads((await outbound.get())["text"])["type"] == "session"
        await task
    resource = resources.sessions[0]
    assert resource.closed
    assert resource.close_code is ErrorCode.SHUTDOWN
    assert resource.events == ["on_close:live", "dependency_close:ended"]
    assert rtc.active_sessions == 0


def test_message_callbacks_use_separate_request_scopes_in_one_session() -> None:
    app, _, resources = application()
    with TestClient(app) as client:
        with client.websocket_connect("/rtc") as websocket:
            assert websocket.receive_json()["type"] == "session"
            received: list[str] = []

            async def offer() -> tuple[RTCPeerConnection, RTCDataChannel, str]:
                remote = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                remote.addTransceiver("audio", direction="recvonly")
                channel = remote.createDataChannel("chat", negotiated=True, id=0)
                channel.on("message", received.append)
                await remote.setLocalDescription(await remote.createOffer())
                return remote, channel, remote.localDescription.sdp

            remote, channel, sdp = client.portal.call(offer)
            websocket.send_json(
                {
                    "type": "description",
                    "description": {"type": "offer", "sdp": sdp},
                    "negotiation": 1,
                }
            )
            while (message := websocket.receive_json())["type"] != "description":
                pass
            client.portal.call(
                remote.setRemoteDescription,
                RTCSessionDescription(**message["description"]),
            )
            while websocket.receive_json()["type"] != "ready":
                pass

            async def exchange(text: str) -> None:
                channel.send(text)
                async with asyncio.timeout(5):
                    while not received or received[-1] != text.upper():
                        await asyncio.sleep(0.01)

            try:
                for text in ("first", "second"):
                    client.portal.call(exchange, text)
                    resource = resources.sessions[0]
                    assert not resource.closed
                    assert all(work.closed for work in resource.work)
                assert len(resource.work) == 2
                assert resource.work[0] is not resource.work[1]
                assert all(work.resource is resource for work in resource.work)
            finally:
                client.portal.call(remote.close)
        assert resource.closed
