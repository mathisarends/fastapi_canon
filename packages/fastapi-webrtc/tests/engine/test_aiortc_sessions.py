"""End-to-end sessions between the aiortc engine and an aiortc client peer."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest
from aiortc import AudioStreamTrack, VideoStreamTrack
from av import AudioFrame, VideoFrame
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Direction,
    ErrorCode,
    IncomingTrack,
    MediaPolicy,
    Peer,
    Role,
    Session,
    SessionEvent,
    SessionLimits,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine
from pydantic import BaseModel
from websockets.exceptions import InvalidStatus

from tests.peer_client import PeerClient
from tests.server import serve

pytestmark = pytest.mark.engine


class Prompt(BaseModel):
    text: str


class Reply(BaseModel):
    text: str
    frames: int = 0


def build_app(
    events: list[SessionEvent] | None = None,
    teardown: list[str] | None = None,
    limits: SessionLimits | None = None,
) -> FastAPI:
    rtc = WebRtcRouter(
        engine=AiortcEngine(),
        prefix="/rtc",
        limits=limits or SessionLimits(negotiation_timeout_seconds=10),
        on_event=None if events is None else events.append,
    )

    async def resource(lesson_id: str) -> AsyncIterator[str]:
        yield f"lesson:{lesson_id}"
        if teardown is not None:
            teardown.append(lesson_id)

    def authorize(token: Annotated[str, Query()] = "") -> str:
        if token != "secret":
            raise HTTPException(status_code=403, detail="forbidden")
        return token

    @rtc.session(
        "/lessons/{lesson_id}",
        name="practice",
        media=MediaPolicy(audio=Direction.SEND_RECEIVE),
        data_channels=(
            DataChannelSpec(name="messages", receive_model=Prompt, send_model=Reply),
        ),
    )
    async def practice(
        lesson: Annotated[str, Depends(resource)],
        _token: Annotated[str, Depends(authorize)],
    ) -> Session:
        frames = 0

        async def on_track(peer: Peer, track: IncomingTrack) -> None:
            nonlocal frames
            await peer.send_track(AudioStreamTrack())
            async for _frame in track:
                frames += 1

        async def on_prompt(peer: Peer, prompt: Prompt) -> None:
            await peer.send_message(
                "messages", Reply(text=f"{lesson}:{prompt.text}", frames=frames)
            )

        return Session(on_track=on_track, on_message={"messages": on_prompt})

    @rtc.session(
        "/broadcast",
        role=Role.OFFERER,
        media=MediaPolicy(video=Direction.SEND),
        data_channels=(DataChannelSpec(name="ticks", send_model=int),),
    )
    def broadcast() -> Session:
        async def run(peer: Peer) -> None:
            for tick in range(3):
                await peer.send_message("ticks", tick)
            await peer.wait_closed()

        return Session(run=run, tracks=[VideoStreamTrack()])

    @rtc.session(
        "/finite", data_channels=(DataChannelSpec(name="text", send_model=str),)
    )
    def finite(websocket: WebSocket) -> Session:
        async def run(peer: Peer) -> None:
            await peer.send_message("text", "bye")
            await asyncio.sleep(0.2)

        return Session(run=run)

    app = FastAPI()
    app.include_router(rtc)
    app.state.rtc = rtc
    return app


async def test_answerer_session_exchanges_media_and_messages() -> None:
    events: list[SessionEvent] = []
    teardown: list[str] = []
    app = build_app(events, teardown)
    async with serve(app) as base:
        url = f"{base}/rtc/lessons/42?token=secret"
        async with PeerClient(url, tracks=[AudioStreamTrack()]) as client:
            assert client.hello["name"] == "practice"
            remote = await asyncio.wait_for(client.remote_tracks.get(), 10)
            assert remote.kind == "audio"
            frame = await asyncio.wait_for(remote.recv(), 10)
            assert isinstance(frame, AudioFrame)
            assert frame.sample_rate > 0
            await asyncio.sleep(0.5)
            client.send_json("messages", {"text": "hi"})
            reply: dict[str, Any] = await client.receive_json("messages")
            assert reply["text"] == "lesson:42:hi"
            assert reply["frames"] > 0
            assert client.renegotiations == 1
            client.send("messages", "not json")
            await asyncio.sleep(0.3)
            assert [e["code"] for e in client.errors] == ["invalid_message"]
        await _until(lambda: teardown == ["42"])
    kinds = [event.kind for event in events]
    assert kinds[:2] == ["admitted", "track"] or kinds[:2] == ["admitted", "connected"]
    assert "ready" in kinds
    assert "renegotiated" in kinds
    assert kinds[-1] == "closed"
    assert events[-1].code is ErrorCode.CLOSED


async def test_dependency_denial_is_an_http_rejection() -> None:
    app = build_app()
    async with serve(app) as base:
        with pytest.raises(InvalidStatus) as error:
            await PeerClient(f"{base}/rtc/lessons/1?token=wrong").connect()
        assert error.value.response.status_code == 403
        assert app.state.rtc.active_sessions == 0


@pytest.mark.parametrize("owned", [False, True])
async def test_source_ownership_survives_real_sender_close(owned: bool) -> None:
    source = AudioStreamTrack()
    rtc = WebRtcRouter(engine=AiortcEngine())

    @rtc.session("/source", media=MediaPolicy(audio=Direction.SEND))
    def session() -> Session:
        async def on_start(peer: Peer) -> None:
            await peer.send_track(source, owned=owned)

        return Session(on_start=on_start)

    app = FastAPI()
    app.include_router(rtc)
    try:
        async with serve(app) as base:
            async with PeerClient(f"{base}/source") as client:
                remote = await asyncio.wait_for(client.remote_tracks.get(), 10)
                assert isinstance(await asyncio.wait_for(remote.recv(), 10), AudioFrame)
            await _until(lambda: rtc.active_sessions == 0)
            assert source.readyState == ("ended" if owned else "live")
            if not owned:
                assert isinstance(await asyncio.wait_for(source.recv(), 1), AudioFrame)
    finally:
        source.stop()


async def test_offerer_session_sends_video_and_messages() -> None:
    app = build_app()
    async with serve(app) as base, PeerClient(f"{base}/rtc/broadcast") as client:
        assert client.hello["role"] == "offerer"
        ticks = [await client.receive_json("ticks") for _ in range(3)]
        assert ticks == [0, 1, 2]
        video = await asyncio.wait_for(client.remote_tracks.get(), 10)
        frame = await asyncio.wait_for(video.recv(), 10)
        assert isinstance(frame, VideoFrame)
        assert frame.width > 0


async def test_returning_run_closes_normally() -> None:
    app = build_app()
    async with serve(app) as base:
        client = PeerClient(f"{base}/rtc/finite")
        await client.connect()
        assert await client.receive("text") == "bye"
        code, reason = await client.wait_closed()
        await client.close()
        assert (code, reason) == (1000, "closed")


async def test_router_close_shuts_sessions_down() -> None:
    events: list[SessionEvent] = []
    app = build_app(events)
    async with serve(app) as base:
        client = PeerClient(f"{base}/rtc/broadcast")
        await client.connect()
        await app.state.rtc.aclose()
        code, reason = await client.wait_closed()
        await client.close()
        assert (code, reason) == (1001, "shutdown")
        assert app.state.rtc.active_sessions == 0
    assert events[-1].code is ErrorCode.SHUTDOWN


async def test_server_shutdown_closes_sessions() -> None:
    events: list[SessionEvent] = []
    app = build_app(events)
    async with serve(app) as base:
        client = PeerClient(f"{base}/rtc/broadcast")
        await client.connect()
    # Uvicorn closes WebSockets with 1012 (service restart) before the lifespan
    # shutdown runs; the session classifies that as a shutdown.
    code, _ = await client.wait_closed()
    await client.close()
    assert code == 1012
    assert events[-1].kind == "closed"
    assert events[-1].code is ErrorCode.SHUTDOWN


async def _until(condition: Any, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)
