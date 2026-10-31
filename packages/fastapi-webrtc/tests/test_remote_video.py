"""Exercise the remote example through real routing and scripted native leases."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi_canon.integrations.webrtc import (
    DescriptionType,
    ErrorCode,
    Session,
    SessionLimits,
    WebRtcError,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engine import (
    ConnectionState,
    IceCandidate,
    PeerConfig,
    PeerObserver,
    SessionDescription,
    SignalingState,
)

from examples.remote_video.app import create_app, create_router
from examples.remote_video.engine import RemoteVideoEngine, VideoLease
from tests.fake_engine import FakeEngine, NativeTrack, sdp
from tests.server import serve
from tests.signaling import Signal, until
from tests.test_runtime import application


class Lease:
    signaling_state: SignalingState = SignalingState.STABLE
    connection_state: ConnectionState = ConnectionState.NEW

    def __init__(
        self, browser_id: str, *, block: bool = False, fail: bool = False
    ) -> None:
        self.browser_id = browser_id
        self.block = block
        self.fail = fail
        self.started = asyncio.Event()
        self.closed = False
        self.opened = False
        self.observer: PeerObserver | None = None
        self.candidates: list[IceCandidate | None] = []
        self.answers: list[SessionDescription] = []

    async def open(self, config: PeerConfig, observer: PeerObserver) -> None:
        self.opened = True
        self.observer = observer
        if self.fail:
            raise WebRtcError(ErrorCode.SESSION_LIMIT)

    async def offer(self) -> SessionDescription:
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        self.signaling_state = SignalingState.HAVE_LOCAL_OFFER
        return SessionDescription(DescriptionType.OFFER, sdp("video sendonly"))

    async def answer(self, description: SessionDescription) -> None:
        self.answers.append(description)
        self.signaling_state = SignalingState.STABLE

    async def candidate(self, candidate: IceCandidate | None) -> None:
        self.candidates.append(candidate)

    async def aclose(self) -> None:
        self.closed = True


class Service:
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.leases: list[Lease] = []
        self.block = block
        self.fail = fail

    @asynccontextmanager
    async def subscribe(self, browser_id: str) -> AsyncIterator[VideoLease]:
        lease = Lease(browser_id, block=self.block, fail=self.fail)
        self.leases.append(lease)
        try:
            yield lease
        finally:
            await lease.aclose()


async def test_rejected_second_viewer_releases_only_its_own_lease() -> None:
    service = Service()
    async with serve(create_app(service)) as base:
        first = await Signal.start(f"{base}/browser/a/video")
        await first.expect("description")
        second = await Signal.open(f"{base}/browser/a/video")
        assert await second.closed() == (1013, "session_limit")
        await until(lambda: service.leases[1].closed)
        assert not service.leases[0].closed
        assert service.leases[0].opened
        assert not service.leases[1].opened
        await first.close()
        await until(lambda: service.leases[0].closed)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_before_offer_releases_the_lease(shutdown: bool) -> None:
    service = Service(block=True)
    router = create_router(service)
    app = application(router)
    async with serve(app) as base:
        signal = await Signal.start(f"{base}/browser/a/video")
        lease = service.leases[0]
        await asyncio.wait_for(lease.started.wait(), 1)
        if shutdown:
            await router.aclose()
            assert await signal.closed() == (1001, "shutdown")
        else:
            await signal.close()
        await until(lambda: lease.closed)


async def test_partial_setup_failure_preserves_category_and_closes_lease() -> None:
    service = Service(fail=True)
    async with serve(create_app(service)) as base:
        signal = await Signal.open(f"{base}/browser/a/video")
        assert await signal.closed() == (1013, "session_limit")
        await until(lambda: service.leases[0].closed)


async def test_session_engine_overrides_router_without_cross_session_state() -> None:
    default = FakeEngine()
    engines = {"a": FakeEngine(), "b": FakeEngine()}
    router = WebRtcRouter(engine=default)

    @router.session("/{key}")
    def session(key: str) -> Session:
        return Session(engine=engines[key])

    async with serve(application(router)) as base:
        first = await Signal.start(f"{base}/a")
        second = await Signal.start(f"{base}/b")
        await first.describe("offer", sdp())
        await first.expect("description")
        await second.describe("offer", sdp())
        await second.expect("description")
        assert len(engines["a"].peers) == len(engines["b"].peers) == 1
        assert default.peers == []
        await second.close()
        await until(lambda: engines["b"].peer.closed)
        assert not engines["a"].peer.closed
        await first.close()
    assert engines["a"].peer.closed


async def test_missing_engine_releases_returned_tracks() -> None:
    router = WebRtcRouter(limits=SessionLimits(negotiation_timeout_seconds=1))
    track = NativeTrack()

    @router.session("/s")
    def session() -> Session:
        return Session(tracks=[track])

    async with serve(application(router)) as base:
        signal = await Signal.open(f"{base}/s")
        assert await signal.closed() == (1011, "handler_failure")
        assert track.stopped


async def test_remote_peer_forwards_native_state_and_partial_stats() -> None:
    lease = Lease("a")
    observer: Any = object()
    engine = RemoteVideoEngine(lease)
    peer = await engine.create_peer(PeerConfig(), observer)
    assert lease.observer is observer
    await peer.create_offer()
    await peer.set_remote_description(
        SessionDescription(DescriptionType.ANSWER, sdp("video recvonly"))
    )
    await peer.add_ice_candidate(None)
    assert peer.signaling_state == "stable"
    assert peer.connection_state == "new"  # An answer does not prove connectivity.
    lease.connection_state = ConnectionState.CONNECTED
    stats = await peer.stats()
    assert stats.connection_state == "connected"
    assert stats.inbound == stats.outbound == ()
    assert stats.round_trip_time_seconds is None
    assert lease.candidates == [None]
    for operation in (
        peer.create_offer(ice_restart=True),
        peer.create_answer(),
        peer.set_remote_description(SessionDescription(DescriptionType.OFFER, sdp())),
    ):
        with pytest.raises(WebRtcError):
            await operation
    await peer.close()
    await peer.close()
    assert lease.closed
