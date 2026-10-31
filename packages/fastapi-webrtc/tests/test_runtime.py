"""Session runtime behavior against a scriptable fake engine."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Never, NoReturn, assert_type

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from fastapi_canon.integrations.webrtc import (
    Channel,
    ConnectionState,
    DataChannelSpec,
    Direction,
    EngineCapabilities,
    ErrorCode,
    IceServer,
    IceServers,
    IncomingTrack,
    MediaKind,
    MediaPolicy,
    Peer,
    Role,
    Session,
    SessionContext,
    SessionEvent,
    SessionEventKind,
    SessionLimits,
    WebRtcError,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.contract import build_contract
from fastapi_canon.integrations.webrtc.engine import PeerConfig, PeerObserver
from pydantic import BaseModel, Field
from websockets.exceptions import InvalidStatus

from tests.fake_engine import (
    FakeEngine,
    FakePeer,
    FakeSender,
    FakeTrack,
    NativeTrack,
    sdp,
)
from tests.server import serve
from tests.signaling import Signal, until


class Prompt(BaseModel):
    text: str = Field(max_length=20)


class Reply(BaseModel):
    text: str


AUDIO = MediaPolicy(audio=Direction.SEND_RECEIVE)
CHAT = DataChannelSpec(name="chat", receive_model=Prompt, send_model=Reply)


def router(
    engine: FakeEngine | None = None,
    *,
    limits: SessionLimits | None = None,
    events: list[SessionEvent] | None = None,
    **kwargs: Any,
) -> WebRtcRouter:
    return WebRtcRouter(
        engine=engine or FakeEngine(),
        limits=limits or SessionLimits(negotiation_timeout_seconds=5),
        on_event=None if events is None else events.append,
        **kwargs,
    )


def application(rtc: WebRtcRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(rtc)
    return app


def engine_of(rtc: WebRtcRouter) -> FakeEngine:
    engine = rtc.engine
    assert isinstance(engine, FakeEngine)
    return engine


async def establish(
    signal: Signal, rtc: WebRtcRouter, sections: Sequence[str] = ()
) -> FakePeer:
    """Complete a browser-offered negotiation and wait for ``ready``."""
    await signal.describe("offer", sdp(*sections))
    await signal.expect("description")
    peer = engine_of(rtc).peer
    peer.connect()
    await signal.expect("ready")
    return peer


# ----------------------------------------------------------------- admission


async def test_dependency_denial_allocates_no_peer() -> None:
    rtc = router()

    def authorize(token: Annotated[str, Query()] = "") -> None:
        if token != "ok":
            raise HTTPException(status_code=403)

    @rtc.session("/s", dependencies=[Depends(authorize)])
    def guarded() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        with pytest.raises(InvalidStatus) as error:
            await Signal.open(f"{base}/s")
        assert error.value.response.status_code == 403
        signal = await Signal.start(f"{base}/s?token=ok")
        await signal.close()
    assert len(engine_of(rtc).peers) == 1


async def test_allowed_origins_refuse_cross_site_handshakes() -> None:
    rtc = router(allowed_origins=["https://App.example.com/"])
    factories: list[str] = []

    @rtc.session("/s")
    def guarded() -> Session:
        factories.append("called")
        return Session()

    async with serve(application(rtc)) as base:
        with pytest.raises(InvalidStatus) as error:
            await Signal.open(f"{base}/s", origin="https://evil.example.com")
        assert error.value.response.status_code == 403
        assert factories == []
        for origin in ("https://app.example.com", None):
            signal = await Signal.start(f"{base}/s", origin=origin)
            await signal.close()
    assert factories == ["called", "called"]
    assert len(engine_of(rtc).peers) == 2


async def test_hello_describes_the_session() -> None:
    rtc = router(limits=SessionLimits(negotiation_timeout_seconds=3))

    @rtc.session(
        "/s",
        media=MediaPolicy(audio=Direction.RECEIVE, max_incoming_tracks=2),
        data_channels=(
            CHAT,
            DataChannelSpec(name="raw", negotiated=False, ordered=False),
            DataChannelSpec(name="blob", send_model=bytes, id=9, max_message_bytes=8),
        ),
    )
    def described() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        assert signal.ws.subprotocol == "fastapi-webrtc.v1"
        hello = signal.hello
        await signal.close()
    assert hello["protocol"] == 1
    assert hello["name"] == "described"
    assert hello["role"] == "answerer"
    assert hello["media"] == {
        "audio": "receive",
        "video": None,
        "max_incoming_tracks": 2,
    }
    channels = {c["name"]: c for c in hello["channels"]}
    assert channels["chat"]["id"] == 0
    assert channels["chat"]["receive_format"] == "json"
    assert channels["raw"]["negotiated"] is False
    assert channels["raw"]["id"] is None
    assert channels["blob"]["id"] == 9
    assert channels["blob"]["send_format"] == "binary"
    assert channels["blob"]["max_message_bytes"] == 8
    assert hello["limits"]["negotiation_timeout_ms"] == 3000
    assert hello["capabilities"] == {
        "trickle_ice": True,
        "ice_restart": False,
        "renegotiation": True,
    }


async def test_unsupported_subprotocol_is_refused_without_a_peer() -> None:
    rtc = router()

    @rtc.session("/s")
    def plain() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.open(f"{base}/s", subprotocols=["fastapi-webrtc.v9"])
        assert await signal.closed() == (1002, "unsupported_protocol")
        # Clients that offer no subprotocol are served.
        signal = await Signal.open(f"{base}/s", subprotocols=[])
        assert (await signal.expect("session"))["name"] == "plain"
        await signal.close()
    assert len(engine_of(rtc).peers) == 1


async def test_session_limits_refuse_extra_sessions() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events, max_sessions=2)

    @rtc.session("/one", max_sessions=1)
    def one() -> Session:
        return Session()

    @rtc.session("/two")
    def two() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        first = await Signal.start(f"{base}/one")
        refused = await Signal.open(f"{base}/one")
        assert await refused.closed() == (1013, "session_limit")
        second = await Signal.start(f"{base}/two")
        refused = await Signal.open(f"{base}/two")
        assert await refused.closed() == (1013, "session_limit")
        await first.close()
        await until(lambda: rtc.active_sessions == 1)
        again = await Signal.start(f"{base}/one")
        await again.close()
        await second.close()
    rejected = [e for e in events if e.kind == "rejected"]
    assert [e.code for e in rejected] == [ErrorCode.SESSION_LIMIT] * 2


async def test_invalid_factories_are_refused() -> None:
    rtc = router()

    @rtc.session("/wrong")
    def wrong() -> Any:
        return object()

    @rtc.session("/handlers", data_channels=(CHAT,))
    def handlers() -> Session:
        async def handle(peer: Peer, message: Any) -> None: ...

        return Session(on_message={"other": handle})

    async with serve(application(rtc)) as base:
        for path in ("/wrong", "/handlers"):
            signal = await Signal.open(f"{base}{path}")
            assert await signal.closed() == (1011, "handler_failure")
    assert engine_of(rtc).peers == []


@pytest.mark.parametrize(
    "rejection", ["router_limit", "endpoint_limit", "shutdown", "handlers"]
)
async def test_refused_sessions_release_tracks_before_dependencies(
    rejection: str,
) -> None:
    rtc = router(max_sessions=1 if rejection == "router_limit" else None)
    tracks: list[NativeTrack] = []
    released: list[bool] = []
    closes: list[ErrorCode] = []

    async def resource() -> AsyncIterator[NativeTrack]:
        track = NativeTrack()
        tracks.append(track)
        try:
            yield track
        finally:
            released.append(track.stopped)

    @rtc.session("/s", max_sessions=1 if rejection == "endpoint_limit" else None)
    async def factory(track: Annotated[NativeTrack, Depends(resource)]) -> Session:
        async def on_close(peer: Peer, code: ErrorCode) -> None:
            closes.append(code)

        handlers = {"undeclared": on_close} if rejection == "handlers" else {}
        return Session(tracks=[track, track], on_message=handlers, on_close=on_close)

    async with serve(application(rtc)) as base:
        if rejection.endswith("limit"):
            accepted = await Signal.start(f"{base}/s")
            expected = (1013, "session_limit")
        elif rejection == "shutdown":
            await rtc.aclose()
            expected = (1001, "shutdown")
        else:
            expected = (1011, "handler_failure")
        for _ in range(2):
            refused = await Signal.open(f"{base}/s")
            assert await refused.closed() == expected
            await until(lambda: len(released) > 0 and tracks[-1].stopped)
        assert closes == []
        if rejection.endswith("limit"):
            assert not tracks[0].stopped
            await accepted.close()
    assert all(track.stopped for track in tracks)
    assert all(released)
    assert len(engine_of(rtc).peers) == (1 if rejection.endswith("limit") else 0)


async def test_engine_failure_closes_with_engine_failure() -> None:
    events: list[SessionEvent] = []
    rtc = router(FakeEngine(fail_create=True), events=events)
    closes: list[ErrorCode] = []

    @rtc.session("/s")
    def failing() -> Session:
        async def on_close(peer: Peer, code: ErrorCode) -> None:
            closes.append(code)

        return Session(on_close=on_close)

    async with serve(application(rtc)) as base:
        signal = await Signal.open(f"{base}/s")
        assert await signal.closed() == (1011, "engine_failure")
    assert closes == [ErrorCode.ENGINE_FAILURE]
    assert [e.kind for e in events] == ["admitted", "closed"]


@pytest.mark.parametrize("stage", ["ice", "engine"])
@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_blocked_startup_is_cancelled_and_releases_resources(
    stage: str, shutdown: bool, suppress_cancel: bool
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    teardown: list[str] = []
    track = NativeTrack()

    async def block() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
        finally:
            cancelled.set()

    class BlockingEngine(FakeEngine):
        async def create_peer(
            self, config: PeerConfig, observer: PeerObserver
        ) -> FakePeer:
            if stage == "engine":
                await block()
            return await super().create_peer(config, observer)

    async def ice(context: SessionContext) -> IceServers:
        if stage == "ice":
            await block()
        return IceServers()

    async def resource() -> AsyncIterator[None]:
        try:
            yield
        finally:
            teardown.append("closed")

    rtc = router(
        BlockingEngine(),
        ice_servers=ice,
        max_sessions=1,
        limits=SessionLimits(negotiation_timeout_seconds=0.2),
    )

    @rtc.session("/blocked", media=AUDIO, dependencies=[Depends(resource)])
    def session() -> Session:
        return Session(tracks=[track])

    async with serve(application(rtc)) as base:
        signal = await Signal.open(f"{base}/blocked")
        await asyncio.wait_for(entered.wait(), 1)
        if shutdown:
            await asyncio.wait_for(rtc.aclose(), 1)
        expected = (1001, "shutdown") if shutdown else (4000, "negotiation_timeout")
        assert await signal.closed(timeout=1) == expected
        await until(lambda: teardown == ["closed"])
        assert cancelled.is_set()
        assert track.stopped
        assert rtc.active_sessions == 0
        if stage == "engine" and suppress_cancel:
            assert len(engine_of(rtc).peers) == 1
            assert engine_of(rtc).peer.closed
        else:
            assert engine_of(rtc).peers == []


# ---------------------------------------------------------------- signaling


@pytest.mark.parametrize("serialization_only", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_outgoing_aliases_match_contract(
    serialization_only: bool, nested: bool
) -> None:
    class Count(BaseModel):
        word_count: int = (
            Field(serialization_alias="wordCount")
            if serialization_only
            else Field(alias="wordCount")
        )

    class Envelope(BaseModel):
        result: Count

    value = Count.model_validate(
        {"word_count" if serialization_only else "wordCount": 3}
    )
    message = Envelope(result=value) if nested else value
    rtc = router()

    @rtc.session(
        "/s", data_channels=(DataChannelSpec(name="counts", send_model=type(message)),)
    )
    def session() -> Session:
        async def on_start(peer: Peer) -> None:
            await peer.send_message("counts", message)

        return Session(on_start=on_start)

    app = application(rtc)
    contract = build_contract(app)
    schema = contract["sessions"][0]["channels"][0]["send_schema"]
    definition = contract["definitions"][schema["$ref"].split("/")[-1]]
    if nested:
        child = definition["properties"]["result"]
        definition = contract["definitions"][child["$ref"].split("/")[-1]]
    assert list(definition["properties"]) == ["wordCount"]
    async with serve(app) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        channel = peer.channels["counts"]
        await until(lambda: channel.sent)
        payload = json.loads(channel.sent[0])
        assert payload == ({"result": {"wordCount": 3}} if nested else {"wordCount": 3})
        await signal.close()


@pytest.mark.parametrize("reason", ["disconnect", "timeout", "attachment"])
async def test_initial_tracks_are_owned_before_attachment(
    reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [NativeTrack(), NativeTrack(), NativeTrack()]
    rtc = router(limits=SessionLimits(negotiation_timeout_seconds=0.2))
    original = FakePeer.add_track

    def add_track(peer: FakePeer, native: Any) -> FakeSender:
        if native is tracks[1]:
            raise RuntimeError("second attachment failed")
        return original(peer, native)

    if reason == "attachment":
        monkeypatch.setattr(FakePeer, "add_track", add_track)

    @rtc.session("/s", media=AUDIO)
    def session() -> Session:
        return Session(tracks=tracks)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        if reason == "disconnect":
            await signal.close()
        else:
            if reason == "attachment":
                await signal.describe("offer", sdp("audio recvonly"))
            expected = (
                "engine_failure" if reason == "attachment" else "negotiation_timeout"
            )
            assert (await signal.closed())[1] == expected
        await until(lambda: rtc.active_sessions == 0)
        assert all(track.stopped for track in tracks)
        assert engine_of(rtc).peer.closed


async def test_browser_offer_is_answered_and_candidates_are_buffered() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events)
    started: list[str] = []

    @rtc.session("/s", media=AUDIO, data_channels=(CHAT,))
    def answerer() -> Session:
        async def on_start(peer: Peer) -> None:
            started.append(peer.name)

        return Session(on_start=on_start)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.candidate("candidate:1 1 udp 1 10.0.0.1 1 typ host")
        await signal.candidate("")  # an empty candidate means end-of-candidates
        await signal.describe("offer", sdp("audio sendrecv"), negotiation=7)
        answer = await signal.expect("description")
        assert answer["negotiation"] == 7
        assert answer["description"]["type"] == "answer"
        peer = engine_of(rtc).peer
        await until(lambda: len(peer.candidates) == 2)
        first, end = peer.candidates
        assert first is not None
        assert first.username_fragment == "remote"
        assert end is None
        # Later candidates are applied directly; stale generations are dropped.
        await signal.candidate("candidate:2 1 udp 1 10.0.0.2 1 typ host")
        await signal.candidate("candidate:3 1 udp 1 10.0.0.3 1 typ host", "old")
        await until(lambda: len(peer.candidates) == 3)
        peer.observer.ice_candidate(None)
        assert (await signal.expect("candidate"))["candidate"] is None
        peer.connect()
        await signal.expect("ready")
        assert started == ["answerer"]
        await signal.close()
    assert len(peer.candidates) == 3
    kinds = [e.kind for e in events]
    assert kinds == ["admitted", "connected", "ready", "closed"]
    assert events[-1].code is ErrorCode.CLOSED
    assert peer.closed
    assert all(c.ready_state == "closed" for c in peer.channels.values())


async def test_local_candidates_are_forwarded_after_the_answer() -> None:
    rtc = router()

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        from fastapi_canon.integrations.webrtc.engine import IceCandidate

        engine_of(rtc).peer.observer.ice_candidate(
            IceCandidate("candidate:9 1 udp 1 10.0.0.9 9 typ host", "0", 0, "local")
        )
        message = await signal.expect("candidate")
        await signal.close()
    assert message["candidate"] == {
        "candidate": "candidate:9 1 udp 1 10.0.0.9 9 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
        "usernameFragment": "local",
    }


async def test_rejected_candidates_are_reported_without_closing() -> None:
    rtc = router()

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        engine_of(rtc).peer.reject_candidates = True
        await signal.candidate("candidate:1 1 udp 1 10.0.0.1 1 typ host")
        error = await signal.expect("error")
        await signal.close()
    assert error["code"] == "negotiation_failed"
    assert error["message"] == "WebRTC negotiation failed."


async def test_too_many_early_candidates_exhaust_the_queue() -> None:
    rtc = router(limits=SessionLimits(max_pending_candidates=2))

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        for index in range(3):
            await signal.candidate(f"candidate:{index} 1 udp 1 10.0.0.1 1 typ host")
        assert await signal.closed() == (4000, "queue_exhausted")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not json", (1008, "protocol_error")),
        ('{"type": "unknown"}', (1008, "protocol_error")),
        ('{"type": "description", "negotiation": 1}', (1008, "protocol_error")),
        (b"\x00binary", (1008, "protocol_error")),
        ("x" * 2048, (1009, "message_too_large")),
    ],
)
async def test_malformed_signaling_closes_the_session(
    payload: str | bytes, expected: tuple[int, str]
) -> None:
    rtc = router(limits=SessionLimits(max_signaling_message_bytes=1024))

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.send(payload)
        assert await signal.closed() == expected
    assert engine_of(rtc).peer.closed


async def test_failed_descriptions_close_with_negotiation_failed() -> None:
    rtc = router()

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        engine_of(rtc).peer.fail_remote = True
        await signal.describe("offer", sdp())
        assert await signal.closed() == (4000, "negotiation_failed")


async def test_server_offers_ignore_stale_answers_and_colliding_offers() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events)
    track = NativeTrack(MediaKind.VIDEO)

    @rtc.session(
        "/s",
        role=Role.OFFERER,
        media=MediaPolicy(audio=Direction.RECEIVE, video=Direction.SEND),
        data_channels=(DataChannelSpec(name="inband", negotiated=False),),
    )
    def offerer() -> Session:
        return Session(tracks=[track])

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        assert signal.hello["role"] == "offerer"
        offer = await signal.expect("description")
        assert offer["description"]["type"] == "offer"
        assert offer["negotiation"] == 1
        peer = engine_of(rtc).peer
        assert peer.transceivers == [("audio", "recvonly")]
        assert [s.kind for s in peer.senders] == ["video"]
        assert "inband" in peer.channels
        # The impolite server keeps its offer when the browser's offer collides.
        await signal.describe("offer", sdp(), negotiation=1)
        await signal.describe("answer", sdp("audio sendonly"), negotiation=2)
        await asyncio.sleep(0.1)
        assert peer.remote_descriptions == []
        await signal.describe("answer", sdp("audio sendonly", "video recvonly"))
        await until(lambda: len(peer.remote_descriptions) == 1)
        assert peer.signaling_state == "stable"
        peer.connect()
        await signal.expect("ready")
        await signal.close()
    assert track.stopped
    assert "renegotiated" not in [e.kind for e in events]


@pytest.mark.parametrize(
    "sections",
    [("video sendrecv",), ("audio sendrecv", "audio sendrecv")],
    ids=["undeclared-kind", "too-many-tracks"],
)
async def test_descriptions_violating_media_policy_are_rejected(
    sections: tuple[str, ...],
) -> None:
    rtc = router()

    @rtc.session(
        "/s", media=MediaPolicy(audio=Direction.RECEIVE, max_incoming_tracks=1)
    )
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp(*sections))
        assert await signal.closed() == (1008, "media_rejected")
    assert engine_of(rtc).peer.remote_descriptions == []


async def test_inactive_and_rejected_sections_are_accepted() -> None:
    rtc = router()

    @rtc.session("/s", media=MediaPolicy(audio=Direction.SEND))
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe(
            "offer", sdp("audio recvonly", "video inactive", "video rejected")
        )
        await signal.expect("description")
        await signal.close()


async def test_signaling_loss_without_grace_terminates() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events)

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        await signal.close(code=4001)
        await until(lambda: rtc.active_sessions == 0)
    assert events[-1].code is ErrorCode.SIGNALING_LOST


async def test_established_media_survives_signaling_loss_for_its_grace() -> None:
    events: list[SessionEvent] = []
    limits = SessionLimits(signaling_loss_grace_seconds=0.3)
    rtc = router(events=events, limits=limits)

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        await signal.close(code=4001)
        await asyncio.sleep(0.1)
        assert rtc.active_sessions == 1
        await until(lambda: rtc.active_sessions == 0)
    assert events[-1].code is ErrorCode.SIGNALING_LOST


# ----------------------------------------------------------------- deadlines


async def test_negotiation_deadline() -> None:
    rtc = router(limits=SessionLimits(negotiation_timeout_seconds=0.2))

    @rtc.session("/s", data_channels=(CHAT,))
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        # The transport connects, but a declared channel never opens.
        engine_of(rtc).peer.set_state(ConnectionState.CONNECTED)
        assert await signal.closed() == (4000, "negotiation_timeout")


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ((ConnectionState.FAILED,), "negotiation_failed"),
        ((ConnectionState.CONNECTED, ConnectionState.FAILED), "transport_lost"),
        ((ConnectionState.CONNECTED, ConnectionState.DISCONNECTED), "transport_lost"),
        ((ConnectionState.CONNECTED, ConnectionState.CLOSED), "transport_lost"),
    ],
)
async def test_transport_failures(
    states: tuple[ConnectionState, ...], expected: str
) -> None:
    rtc = router(limits=SessionLimits(transport_loss_grace_seconds=0.1))

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        for state in states:
            engine_of(rtc).peer.set_state(state)
        assert await signal.closed() == (4000, expected)


async def test_transport_recovering_within_grace_keeps_the_session() -> None:
    rtc = router(limits=SessionLimits(transport_loss_grace_seconds=0.2))

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        peer.set_state(ConnectionState.DISCONNECTED)
        await asyncio.sleep(0.05)
        peer.set_state(ConnectionState.CONNECTED)
        await asyncio.sleep(0.3)
        assert rtc.active_sessions == 1
        await signal.close()


async def test_ice_restart_engines_get_a_grace_after_failure() -> None:
    capabilities = EngineCapabilities(
        trickle_ice=True, ice_restart=True, renegotiation=True
    )
    rtc = router(
        FakeEngine(capabilities=capabilities),
        limits=SessionLimits(transport_loss_grace_seconds=0.1),
    )

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        peer.set_state(ConnectionState.FAILED)
        assert rtc.active_sessions == 1
        assert await signal.closed() == (4000, "transport_lost")


async def test_media_deadline() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events, limits=SessionLimits(media_timeout_seconds=0.3))

    @rtc.session("/s", media=AUDIO)
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc, ["audio sendrecv"])
        assert await signal.closed() == (4000, "media_timeout")

        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp("audio sendrecv"))
        await signal.expect("description")
        peer = engine_of(rtc).peer
        peer.received_packets[MediaKind.AUDIO] = 3
        peer.connect()
        await signal.expect("ready")
        await until(lambda: events[-1].kind == "media_ready")
        await signal.close()


# ------------------------------------------------------------------ handlers


async def test_handler_failure_runs_on_close_before_dependency_teardown() -> None:
    order: list[str] = []
    rtc = router()

    async def resource() -> AsyncIterator[str]:
        yield "lesson"
        order.append("teardown")

    @rtc.session("/s")
    def failing(lesson: Annotated[str, Depends(resource)]) -> Session:
        async def run(peer: Peer) -> None:
            raise RuntimeError("boom")

        async def on_close(peer: Peer, code: ErrorCode) -> None:
            order.append(f"on_close:{code}:{lesson}")

        return Session(run=run, on_close=on_close)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        engine_of(rtc).peer.connect()
        assert await signal.closed() == (1011, "handler_failure")
        await until(lambda: "teardown" in order)
    assert order == ["on_close:handler_failure:lesson", "teardown"]


async def test_returning_run_closes_normally_and_peer_close_uses_its_code() -> None:
    rtc = router()

    @rtc.session("/done")
    def done() -> Session:
        async def run(peer: Peer) -> None:
            return None

        return Session(run=run)

    @rtc.session("/closer")
    def closer() -> Session:
        async def on_start(peer: Peer) -> None:
            peer.close(ErrorCode.MEDIA_REJECTED)
            assert await peer.wait_closed() is ErrorCode.MEDIA_REJECTED

        return Session(on_start=on_start)

    async with serve(application(rtc)) as base:
        for path, expected in (
            ("/done", (1000, "closed")),
            ("/closer", (1008, "media_rejected")),
        ):
            signal = await Signal.start(f"{base}{path}")
            await signal.describe("offer", sdp())
            await signal.expect("description")
            engine_of(rtc).peer.connect()
            assert await signal.closed() == expected


async def test_typed_messages_round_trip_and_report_invalid_input() -> None:
    rtc = router()

    @rtc.session(
        "/s",
        data_channels=(
            CHAT,
            DataChannelSpec(name="text", receive_model=str, send_model=str),
            DataChannelSpec(name="blob", receive_model=bytes, send_model=bytes),
            DataChannelSpec(name="raw", max_message_bytes=4),
        ),
    )
    def messages() -> Session:
        async def on_chat(peer: Peer, prompt: Prompt) -> None:
            await peer.send_message("chat", Reply(text=prompt.text.upper()))

        async def on_text(peer: Peer, text: str) -> None:
            await peer.channel("text").send(text[::-1])

        async def on_blob(peer: Peer, data: bytes) -> None:
            await peer.send_message("blob", data * 2)

        async def on_raw(peer: Peer, data: str | bytes) -> None:
            await peer.send_message("raw", data)

        return Session(
            on_message={
                "chat": on_chat,
                "text": on_text,
                "blob": on_blob,
                "raw": on_raw,
            }
        )

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        chat, text, blob, raw = (
            peer.channels[n] for n in ("chat", "text", "blob", "raw")
        )
        chat.deliver('{"text": "hi"}')
        text.deliver("abc")
        blob.deliver(b"\x01")
        raw.deliver(b"ok")
        await until(lambda: chat.sent and text.sent and blob.sent and raw.sent)
        assert json.loads(chat.sent[0]) == {"text": "HI"}
        assert text.sent == ["cba"]
        assert blob.sent == [b"\x01\x01"]
        assert raw.sent == [b"ok"]
        chat.deliver('{"text": "this prompt is far too long"}')
        chat.deliver(b"{}")
        text.deliver(b"bytes")
        blob.deliver("text")
        raw.deliver("too long")
        for _ in range(5):
            await signal.expect("error")
        await signal.close()
    errors = [(e["code"], e["channel"]) for e in signal.of("error")]
    assert errors == [
        ("invalid_message", "chat"),
        ("invalid_message", "chat"),
        ("invalid_message", "text"),
        ("invalid_message", "blob"),
        ("message_too_large", "raw"),
    ]


async def test_channel_specs_give_typed_handles() -> None:
    received: list[Prompt] = []
    rtc = router()
    events = DataChannelSpec(name="events", send_model=Reply)

    @rtc.session("/s", data_channels=(CHAT, events))
    def typed() -> Session:
        async def on_chat(peer: Peer, prompt: Prompt) -> None:
            received.append(prompt)
            channel = peer.channel(CHAT)
            assert_type(channel, Channel[Prompt, Reply])
            await channel.send(Reply(text=prompt.text.upper()))
            await peer.send_message(events, Reply(text="sent"))

        async def run(peer: Peer) -> None:
            foreign = DataChannelSpec(name="events", send_model=Prompt)
            with pytest.raises(ValueError, match="different DataChannelSpec"):
                peer.channel(foreign)
            await peer.wait_closed()

        return Session(run=run, on_message={CHAT: on_chat})

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        peer.channels["chat"].deliver('{"text": "hi"}')
        await until(lambda: peer.channels["events"].sent)
        assert json.loads(peer.channels["chat"].sent[0]) == {"text": "HI"}
        assert json.loads(peer.channels["events"].sent[0]) == {"text": "sent"}
        await signal.close()
    assert received == [Prompt(text="hi")]


async def test_typed_channels_can_be_iterated() -> None:
    received: list[str] = []
    rtc = router()
    notes = DataChannelSpec(name="notes", receive_model=str)

    @rtc.session("/s", data_channels=(notes,))
    def reader() -> Session:
        async def run(peer: Peer) -> None:
            async for note in peer.channel(notes):
                assert_type(note, str)
                received.append(note)

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        peer.channels["notes"].deliver("one")
        peer.channels["notes"].deliver("two")
        await until(lambda: len(received) == 2)
        await signal.close()
    assert received == ["one", "two"]


async def test_one_way_channels_drop_or_refuse_the_other_direction() -> None:
    failures: list[BaseException] = []
    rtc = router()
    ticks: DataChannelSpec[Never, str] = DataChannelSpec(
        "ticks", receive_model=Never, send_model=str
    )
    uploads = DataChannelSpec("uploads", receive_model=str, send_model=NoReturn)

    @rtc.session("/s", data_channels=(ticks, uploads))
    def one_way() -> Session:
        async def run(peer: Peer) -> None:
            await peer.send_message(ticks, "tick")
            for attempt in (
                peer.send_message(uploads, "back"),
                anext(aiter(peer.channel(ticks))),
            ):
                try:
                    await attempt
                except (WebRtcError, RuntimeError) as error:
                    failures.append(error)
            await peer.wait_closed()

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        await until(lambda: len(failures) == 2)
        peer.channels["ticks"].deliver("tock")
        await signal.expect("error")
        await signal.close()
    assert peer.channels["ticks"].sent == ["tick"]
    assert peer.channels["uploads"].sent == []
    sent, received = failures
    assert isinstance(sent, WebRtcError) and sent.code is ErrorCode.INVALID_MESSAGE
    assert isinstance(received, RuntimeError)
    assert "does not receive" in str(received)
    errors = [(e["code"], e["channel"]) for e in signal.of("error")]
    assert errors == [("invalid_message", "ticks")]
    described = signal.hello["channels"]
    assert [c["receive_format"] for c in described] == ["none", "text"]
    assert [c["send_format"] for c in described] == ["text", "none"]


async def test_handlers_for_send_only_channels_are_refused() -> None:
    rtc = router()
    ticks = DataChannelSpec("ticks", receive_model=Never, send_model=str)

    @rtc.session("/s", data_channels=(ticks,))
    def misdeclared() -> Session:
        async def on_tick(peer: Peer, tick: Any) -> None:
            raise AssertionError("unreachable")

        return Session(on_message={"ticks": on_tick})

    async with serve(application(rtc)) as base:
        refused = await Signal.open(f"{base}/s")
        assert await refused.closed() == (1011, "handler_failure")


async def test_handlers_for_foreign_specs_are_refused() -> None:
    rtc = router()
    foreign = DataChannelSpec(name="chat", receive_model=Reply)

    @rtc.session("/s", data_channels=(CHAT,))
    def mismatched() -> Session:
        async def on_reply(peer: Peer, reply: Reply) -> None:
            raise AssertionError("unreachable")

        return Session(on_message={foreign: on_reply})

    async with serve(application(rtc)) as base:
        refused = await Signal.open(f"{base}/s")
        assert await refused.closed() == (1011, "handler_failure")
    assert engine_of(rtc).peers == []


async def test_send_errors_are_raised_to_the_handler() -> None:
    outcomes: list[str] = []
    rtc = router()

    @rtc.session(
        "/s",
        data_channels=(
            DataChannelSpec(name="reply", send_model=Reply, max_message_bytes=32),
            DataChannelSpec(name="text", send_model=str),
            DataChannelSpec(name="blob", send_model=bytes),
            DataChannelSpec(name="raw"),
        ),
    )
    def sender() -> Session:
        async def attempt(peer: Peer, channel: str, message: Any) -> None:
            try:
                await peer.send_message(channel, message)
                outcomes.append("sent")
            except WebRtcError as error:
                outcomes.append(error.code.value)

        async def run(peer: Peer) -> None:
            await attempt(peer, "reply", {"text": 1})
            await attempt(peer, "reply", Reply(text="x" * 64))
            await attempt(peer, "text", b"bytes")
            await attempt(peer, "blob", "text")
            await attempt(peer, "raw", 1)
            await attempt(peer, "raw", bytearray(b"ok"))
            await attempt(peer, "reply", {"text": "ok"})
            with pytest.raises(ValueError, match="no channel"):
                await peer.send_message("missing", "x")
            await peer.wait_closed()

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        await until(lambda: len(outcomes) == 7)
        await signal.close()
    assert outcomes == [
        "invalid_message",
        "message_too_large",
        "invalid_message",
        "invalid_message",
        "invalid_message",
        "sent",
        "sent",
    ]
    assert peer.channels["raw"].sent == [b"ok"]


async def test_sending_on_a_closed_channel() -> None:
    outcomes: list[str] = []
    rtc = router()

    @rtc.session("/s", data_channels=(DataChannelSpec(name="out", send_model=str),))
    def sender() -> Session:
        async def on_start(peer: Peer) -> None:
            assert peer.channel("out").is_open

        async def run(peer: Peer) -> None:
            channel = peer.channel("out")
            await until(lambda: not channel.is_open)
            try:
                await channel.send("late")
            except WebRtcError as error:
                outcomes.append(error.code.value)
            await peer.wait_closed()

        return Session(on_start=on_start, run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        peer.channels["out"].close()
        await until(lambda: outcomes == ["channel_closed"])
        await signal.close()


async def test_engine_send_failures() -> None:
    outcomes: list[str] = []
    rtc = router()

    @rtc.session("/s", data_channels=(DataChannelSpec(name="out", send_model=str),))
    def sender() -> Session:
        async def run(peer: Peer) -> None:
            try:
                await peer.send_message("out", "x")
            except WebRtcError as error:
                outcomes.append(error.code.value)
            await peer.wait_closed()

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        peer = engine_of(rtc).peer
        peer.channels["out"].fail_send = True
        peer.connect()
        await until(lambda: outcomes == ["engine_failure"])
        await signal.close()


async def test_send_waits_for_buffer_space() -> None:
    rtc = router(limits=SessionLimits(send_buffer_high_bytes=10))

    @rtc.session("/s", data_channels=(DataChannelSpec(name="out", send_model=str),))
    def sender() -> Session:
        async def run(peer: Peer) -> None:
            await peer.send_message("out", "first")
            await peer.wait_closed()

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        peer = engine_of(rtc).peer
        channel = peer.channels["out"]
        assert channel.threshold == 10
        channel.buffered_amount = 100
        peer.connect()
        await signal.expect("ready")
        await asyncio.sleep(0.1)
        assert channel.sent == []
        channel.drain()
        await until(lambda: channel.sent == ["first"])
        await signal.close()


async def test_slow_consumers_are_terminated() -> None:
    limits = SessionLimits(send_buffer_high_bytes=10, send_timeout_seconds=0.2)
    rtc = router(limits=limits)

    @rtc.session("/s", data_channels=(DataChannelSpec(name="out", send_model=str),))
    def sender() -> Session:
        async def run(peer: Peer) -> None:
            await peer.send_message("out", "stuck")

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        peer = engine_of(rtc).peer
        peer.channels["out"].buffered_amount = 100
        peer.connect()
        assert await signal.closed() == (4000, "slow_consumer")


async def test_inbound_queue_overflow() -> None:
    release = asyncio.Event()
    rtc = router(limits=SessionLimits(max_queued_messages=1))

    @rtc.session("/s", data_channels=(DataChannelSpec(name="in", receive_model=str),))
    def receiver() -> Session:
        async def on_message(peer: Peer, text: str) -> None:
            await release.wait()

        return Session(on_message={"in": on_message})

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        channel = peer.channels["in"]
        channel.deliver("busy")
        await asyncio.sleep(0.05)
        channel.deliver("queued")
        channel.deliver("overflow")
        assert await signal.closed() == (4000, "queue_exhausted")


async def test_channels_without_handlers_are_iterable() -> None:
    received: list[str] = []
    rtc = router()

    @rtc.session("/s", data_channels=(DataChannelSpec(name="in", receive_model=str),))
    def reader() -> Session:
        async def run(peer: Peer) -> None:
            channel = peer.channel("in")
            async for text in channel:
                received.append(text)
                if text == "stop":
                    break
            async for text in channel:
                received.append(f"again:{text}")
            assert channel.name == "in"
            assert channel.spec.receive_model is str
            assert channel.buffered_amount == 0

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        channel = peer.channels["in"]
        for text in ("a", "stop", "b"):
            channel.deliver(text)
        await until(lambda: len(received) == 3)
        channel.close()
        assert await signal.closed() == (1000, "closed")
    assert received == ["a", "stop", "again:b"]


async def test_handler_channels_cannot_be_iterated() -> None:
    errors: list[str] = []
    rtc = router()

    @rtc.session("/s", data_channels=(DataChannelSpec(name="in", receive_model=str),))
    def reader() -> Session:
        async def on_message(peer: Peer, text: str) -> None: ...

        async def run(peer: Peer) -> None:
            try:
                async for _ in peer.channel("in"):
                    pass
            except RuntimeError as error:
                errors.append(str(error))
            await peer.wait_closed()

        return Session(on_message={"in": on_message}, run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        await until(lambda: errors)
        await signal.close()
    assert "on_message" in errors[0]


async def test_undeclared_remote_channels_are_rejected() -> None:
    rtc = router()

    @rtc.session("/s", data_channels=(CHAT,))
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc)
        intruder = peer.remote_channel("intruder")
        # A pre-negotiated name cannot be claimed by an in-band channel either.
        duplicate = peer.remote_channel("chat")
        first = await signal.expect("error")
        second = await signal.expect("error")
        await signal.close()
    assert (first["code"], first["channel"]) == ("channel_rejected", "intruder")
    assert (second["code"], second["channel"]) == ("channel_rejected", "chat")
    assert intruder.ready_state == duplicate.ready_state == "closed"


async def test_in_band_channels_opened_by_the_browser_are_bound() -> None:
    received: list[str] = []
    rtc = router()

    @rtc.session(
        "/s",
        data_channels=(
            DataChannelSpec(name="late", receive_model=str, negotiated=False),
        ),
    )
    def answerer() -> Session:
        async def on_late(peer: Peer, text: str) -> None:
            received.append(text)

        return Session(on_message={"late": on_late})

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp())
        await signal.expect("description")
        peer = engine_of(rtc).peer
        assert peer.channels == {}
        channel = peer.remote_channel("late")
        peer.set_state(ConnectionState.CONNECTED)
        await asyncio.sleep(0.05)
        assert not signal.of("ready")
        channel.open()
        await signal.expect("ready")
        channel.deliver("hello")
        await until(lambda: received == ["hello"])
        await signal.close()


# --------------------------------------------------------------------- media


async def test_incoming_tracks_are_dispatched_and_stopped() -> None:
    events: list[SessionEvent] = []
    frames: list[Any] = []
    rtc = router(events=events)

    @rtc.session("/s", media=AUDIO)
    def listener() -> Session:
        async def on_track(peer: Peer, track: IncomingTrack) -> None:
            assert track.kind == "audio"
            assert track in peer.incoming_tracks
            assert track.native is not None
            async for frame in track:
                frames.append(frame)

        return Session(on_track=on_track)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.describe("offer", sdp("audio sendrecv"))
        await signal.expect("description")
        peer = engine_of(rtc).peer
        remote = FakeTrack(MediaKind.AUDIO)
        # Tracks that arrive before the session is ready are queued.
        peer.observer.track(remote)
        remote.frames.put_nowait("frame-1")
        peer.connect()
        await until(lambda: frames == ["frame-1"])
        remote.frames.put_nowait("frame-2")
        await until(lambda: len(frames) == 2)
        await signal.close()
    assert remote.stopped
    assert [e.media_kind for e in events if e.kind == "track"] == ["audio"]


async def test_unhandled_tracks_are_drained() -> None:
    rtc = router()

    @rtc.session("/s", media=AUDIO)
    def ignorer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc, ["audio sendrecv"])
        remote = FakeTrack(MediaKind.AUDIO)
        peer.observer.track(remote)
        for index in range(3):
            remote.frames.put_nowait(index)
        await until(lambda: remote.frames.empty())
        await signal.close()


@pytest.mark.parametrize(
    "kinds",
    [(MediaKind.VIDEO,), (MediaKind.AUDIO, MediaKind.AUDIO)],
    ids=["undeclared-kind", "too-many-tracks"],
)
async def test_tracks_violating_media_policy(kinds: tuple[MediaKind, ...]) -> None:
    rtc = router()

    @rtc.session(
        "/s", media=MediaPolicy(audio=Direction.RECEIVE, max_incoming_tracks=1)
    )
    def listener() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc, ["audio sendonly"])
        tracks = [FakeTrack(kind) for kind in kinds]
        for track in tracks:
            peer.observer.track(track)
        assert await signal.closed() == (1008, "media_rejected")
    assert tracks[-1].stopped


@pytest.mark.parametrize("ready", [False, True])
async def test_ended_tracks_release_capacity_exactly_once(ready: bool) -> None:
    rtc = router()

    @rtc.session(
        "/s",
        media=MediaPolicy(
            audio=Direction.RECEIVE, video=Direction.RECEIVE, max_incoming_tracks=1
        ),
    )
    def listener() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        if ready:
            peer = await establish(signal, rtc, ["audio sendonly", "video sendonly"])
        else:
            peer = engine_of(rtc).peer
        video = FakeTrack(MediaKind.VIDEO)
        peer.observer.track(video)
        for _ in range(3):
            audio = FakeTrack(MediaKind.AUDIO)
            peer.observer.track(audio)
            assert not audio.stopped
            audio.end()
            audio.end()  # Duplicate engine notifications must not free two slots.
        current = FakeTrack(MediaKind.AUDIO)
        peer.observer.track(current)
        assert not current.stopped
        assert not video.stopped
        if not ready:
            await establish(signal, rtc, ["audio sendonly", "video sendonly"])
        extra = FakeTrack(MediaKind.AUDIO)
        peer.observer.track(extra)
        assert await signal.closed() == (1008, "media_rejected")
        assert extra.stopped


async def test_tracks_added_after_negotiation_renegotiate() -> None:
    events: list[SessionEvent] = []
    owned = NativeTrack(MediaKind.AUDIO)
    borrowed = NativeTrack(MediaKind.AUDIO)
    steps: list[str] = []
    rtc = router(events=events)

    @rtc.session("/s", media=MediaPolicy(audio=Direction.SEND))
    def speaker() -> Session:
        async def run(peer: Peer) -> None:
            handle = await peer.send_track(owned)
            steps.append(f"sent:{handle.kind}:{handle.owned}")
            await peer.send_track(borrowed, owned=False)
            steps.append("borrowed")
            await handle.remove()
            await handle.remove()
            steps.append("removed")
            for track, error in (
                (NativeTrack(MediaKind.VIDEO), "media_rejected"),
                ("not a track", "media_rejected"),
            ):
                try:
                    await peer.send_track(track)
                except WebRtcError as failure:
                    steps.append(f"{failure.code.value}")
                    assert failure.code.value == error
            stats = await peer.stats()
            steps.append(stats.connection_state)
            await peer.wait_closed()

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        peer = await establish(signal, rtc, ["audio recvonly"])
        for negotiation in (1, 2, 3):
            offer = await signal.expect("description")
            assert offer["negotiation"] == negotiation
            assert offer["description"]["type"] == "offer"
            await signal.describe("answer", sdp("audio recvonly"), negotiation)
        await until(lambda: len(steps) == 6)
        assert owned.stopped
        assert not borrowed.stopped
        await signal.close()
    assert steps == [
        "sent:audio:True",
        "borrowed",
        "removed",
        "media_rejected",
        "media_rejected",
        "connected",
    ]
    assert not borrowed.stopped
    assert [e.kind for e in events].count(SessionEventKind.RENEGOTIATED) == 3
    assert [s.track for s in peer.senders] == [borrowed]


async def test_browser_renegotiation_is_answered() -> None:
    events: list[SessionEvent] = []
    rtc = router(events=events)

    @rtc.session("/s", media=AUDIO)
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        await signal.describe("offer", sdp("audio sendrecv"), negotiation=2)
        answer = await signal.expect("description")
        await signal.close()
    assert answer["negotiation"] == 2
    assert "renegotiated" in [e.kind for e in events]


async def test_engines_without_renegotiation_refuse_late_tracks() -> None:
    capabilities = EngineCapabilities(
        trickle_ice=False, ice_restart=False, renegotiation=False
    )
    rtc = router(FakeEngine(capabilities=capabilities))

    @rtc.session("/s", media=MediaPolicy(audio=Direction.SEND))
    def speaker() -> Session:
        async def run(peer: Peer) -> None:
            await peer.send_track(NativeTrack(MediaKind.AUDIO))

        return Session(run=run)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        assert await signal.closed() == (1011, "handler_failure")


# -------------------------------------------------------------- ICE servers


async def test_ice_servers_from_providers() -> None:
    contexts: list[SessionContext] = []
    public = IceServer(urls=("turn:public.example:3478",), username="u", credential="c")
    internal = IceServer(urls=("turn:10.0.0.1:3478",), username="u", credential="c")

    async def provider(context: SessionContext) -> IceServers:
        contexts.append(context)
        return IceServers(client=(public,), engine=(internal,))

    rtc = router(ice_servers=provider)
    override = IceServer(urls=("stun:override.example",))

    @rtc.session("/default")
    def default() -> Session:
        return Session()

    @rtc.session("/override")
    def overridden() -> Session:
        return Session(ice_servers=[override])

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/default")
        hello = signal.hello
        await signal.close()
        assert hello["ice_servers"] == [
            {"urls": ["turn:public.example:3478"], "username": "u", "credential": "c"}
        ]
        assert engine_of(rtc).peer.config.ice_servers == (internal,)
        signal = await Signal.start(f"{base}/override")
        assert signal.hello["ice_servers"] == [
            {"urls": ["stun:override.example"], "username": None, "credential": None}
        ]
        await signal.close()
    assert [c.name for c in contexts] == ["default"]
    assert contexts[0].session_id == hello["session_id"]


async def test_failing_ice_providers_close_the_session() -> None:
    def provider(context: SessionContext) -> list[IceServer]:
        raise RuntimeError("TURN service unavailable")

    rtc = router(ice_servers=provider)

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.open(f"{base}/s")
        assert await signal.closed() == (1011, "handler_failure")
    assert engine_of(rtc).peers == []


# ------------------------------------------------------------------ shutdown


def test_asgi_cancellation_during_cleanup_keeps_dependencies_alive() -> None:
    rtc = router()
    events: list[str] = []
    closing = asyncio.Event()
    track = NativeTrack()

    async def resource() -> AsyncIterator[None]:
        yield
        events.append(f"dependency_close:{track.stopped}")

    @rtc.session("/s", dependencies=[Depends(resource)])
    def factory() -> Session:
        async def on_close(peer: Peer, code: ErrorCode) -> None:
            closing.set()
            await asyncio.sleep(0.05)
            assert events == []
            events.append("on_close")

        return Session(tracks=[track], on_close=on_close)

    with TestClient(application(rtc)) as client:
        assert client.portal is not None
        with client.websocket_connect("/s") as websocket:
            assert websocket.receive_json()["type"] == "session"
            websocket.close()
            client.portal.call(closing.wait)
        assert events == ["on_close", "dependency_close:True"]
        assert rtc.active_sessions == 0


async def test_router_close_shuts_sessions_down_and_refuses_new_ones() -> None:
    closes: list[ErrorCode] = []
    rtc = router()

    @rtc.session("/s")
    def answerer() -> Session:
        async def on_close(peer: Peer, code: ErrorCode) -> None:
            closes.append(code)

        return Session(on_close=on_close)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await rtc.aclose()
        assert await signal.closed() == (1001, "shutdown")
        refused = await Signal.open(f"{base}/s")
        assert await refused.closed() == (1001, "shutdown")
    assert closes == [ErrorCode.SHUTDOWN]
    assert rtc.active_sessions == 0


async def test_cleanup_tolerates_stuck_and_failing_handlers() -> None:
    rtc = router(limits=SessionLimits(shutdown_timeout_seconds=0.2))

    @rtc.session("/s")
    def stubborn() -> Session:
        async def on_close(peer: Peer, code: ErrorCode) -> None:
            await asyncio.sleep(10)

        return Session(on_close=on_close)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await signal.close()
        await until(lambda: rtc.active_sessions == 0, timeout=3)
    assert engine_of(rtc).peer.closed


async def test_on_close_runs_after_stuck_handlers_used_the_deadline() -> None:
    closes: list[ErrorCode] = []
    release = asyncio.Event()
    rtc = router(limits=SessionLimits(shutdown_timeout_seconds=0.1))

    @rtc.session("/s")
    def stuck() -> Session:
        async def run(peer: Peer) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        async def on_close(peer: Peer, code: ErrorCode) -> None:
            await asyncio.sleep(0.05)
            closes.append(code)

        return Session(run=run, on_close=on_close)

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        await establish(signal, rtc)
        await signal.close()
        await until(lambda: rtc.active_sessions == 0, timeout=3)
        release.set()
    assert closes == [ErrorCode.CLOSED]
    assert engine_of(rtc).peer.closed


async def test_event_hook_failures_are_ignored() -> None:
    def hook(event: SessionEvent) -> None:
        raise RuntimeError("metrics down")

    rtc = WebRtcRouter(engine=FakeEngine(), on_event=hook, max_sessions=1)

    @rtc.session("/s")
    def answerer() -> Session:
        return Session()

    async with serve(application(rtc)) as base:
        signal = await Signal.start(f"{base}/s")
        refused = await Signal.open(f"{base}/s")
        assert await refused.closed() == (1013, "session_limit")
        await establish(signal, rtc)
        await signal.close()
