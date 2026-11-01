"""A scriptable in-memory engine for runtime tests.

Tests drive the peer directly: they open the transport, open channels, emit
candidates and tracks, and inspect what the runtime asked the engine to do.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi_canon.integrations.webrtc.engine import (
    ChannelParameters,
    ChannelState,
    ConnectionState,
    ConnectionStats,
    DescriptionType,
    EngineCapabilities,
    IceCandidate,
    MediaKind,
    MediaStats,
    PeerConfig,
    PeerObserver,
    SessionDescription,
    SignalingState,
    TransceiverDirection,
)

_ids = itertools.count()


def sdp(*sections: str, ufrag: str = "remote") -> str:
    """Build a minimal SDP: each section is ``"<kind> <direction>"`` or
    ``"<kind> rejected"``."""
    lines = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=-", "t=0 0"]
    for index, section in enumerate(sections):
        kind, direction = section.split()
        port = 0 if direction == "rejected" else 9
        lines.append(f"m={kind} {port} UDP/TLS/RTP/SAVPF 0")
        lines.append(f"a=mid:{index}")
        lines.append(f"a=ice-ufrag:{ufrag}")
        if direction != "rejected":
            lines.append(f"a={direction}")
    return "\r\n".join(lines) + "\r\n"


class NativeTrack:
    """A local track handed to ``Peer.send_track`` or ``Session.tracks``."""

    def __init__(self, kind: MediaKind = MediaKind.AUDIO) -> None:
        self.kind = kind
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeTrack:
    """A remote track emitted by the fake engine."""

    def __init__(self, kind: MediaKind = MediaKind.AUDIO) -> None:
        self.id = f"track-{next(_ids)}"
        self.kind: MediaKind = kind
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.stopped = False
        self._ended: list[Callable[[], None]] = []

    @property
    def native(self) -> Any:
        return self

    async def recv(self) -> Any:
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    def on_ended(self, callback: Callable[[], None]) -> None:
        self._ended.append(callback)

    def end(self) -> None:
        for callback in self._ended:
            callback()
        self.frames.put_nowait(RuntimeError("ended"))

    def stop(self) -> None:
        self.stopped = True
        self.end()


@dataclass
class FakeSender:
    kind: MediaKind
    track: Any


class FakeChannel:
    def __init__(self, label: str, channel_id: int | None = None) -> None:
        self.label = label
        self.id = channel_id
        self.ready_state: ChannelState = ChannelState.CONNECTING
        self.buffered_amount = 0
        self.threshold: int | None = None
        self.sent: list[str | bytes] = []
        self.fail_send = False
        self._on_open: Callable[[], None] | None = None
        self._on_message: Callable[[str | bytes], None] | None = None
        self._on_close: Callable[[], None] | None = None
        self._on_low: Callable[[], None] | None = None

    def set_buffered_amount_low_threshold(self, value: int) -> None:
        self.threshold = value

    def observe(
        self,
        *,
        on_open: Callable[[], None],
        on_message: Callable[[str | bytes], None],
        on_close: Callable[[], None],
        on_buffered_amount_low: Callable[[], None],
    ) -> None:
        self._on_open = on_open
        self._on_message = on_message
        self._on_close = on_close
        self._on_low = on_buffered_amount_low

    def send(self, data: str | bytes) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(data)

    def close(self) -> None:
        if self.ready_state != ChannelState.CLOSED:
            self.ready_state = ChannelState.CLOSED
            if self._on_close is not None:
                self._on_close()

    # Test controls -------------------------------------------------------

    def open(self) -> None:
        self.ready_state = ChannelState.OPEN
        if self._on_open is not None:
            self._on_open()

    def deliver(self, data: str | bytes) -> None:
        assert self._on_message is not None
        self._on_message(data)

    def drain(self) -> None:
        self.buffered_amount = 0
        if self._on_low is not None:
            self._on_low()


class FakePeer:
    def __init__(
        self, engine: FakeEngine, config: PeerConfig, observer: PeerObserver
    ) -> None:
        self.engine = engine
        self.config = config
        self.observer = observer
        self.signaling_state: SignalingState = SignalingState.STABLE
        self.connection_state: ConnectionState = ConnectionState.NEW
        self.remote_descriptions: list[SessionDescription] = []
        self.local_descriptions: list[SessionDescription] = []
        self.candidates: list[IceCandidate | None] = []
        self.transceivers: list[tuple[MediaKind, TransceiverDirection]] = []
        self.senders: list[FakeSender] = []
        self.channels: dict[str, FakeChannel] = {}
        self.dirty = False
        self.closed = False
        self.reject_candidates = False
        self.fail_remote = False
        self.received_packets: dict[MediaKind, int] = {
            MediaKind.AUDIO: 0,
            MediaKind.VIDEO: 0,
        }

    async def set_remote_description(self, description: SessionDescription) -> None:
        if self.fail_remote:
            raise ValueError("bad description")
        self.remote_descriptions.append(description)
        if description.type == DescriptionType.OFFER:
            self.signaling_state = SignalingState.HAVE_REMOTE_OFFER
        else:
            self.signaling_state = SignalingState.STABLE

    async def create_offer(self, *, ice_restart: bool = False) -> SessionDescription:
        self.dirty = False
        sections = [f"{kind} {direction}" for kind, direction in self.transceivers]
        sections += [f"{sender.kind} sendonly" for sender in self.senders]
        offer = SessionDescription(DescriptionType.OFFER, sdp(*sections, ufrag="local"))
        self.signaling_state = SignalingState.HAVE_LOCAL_OFFER
        self.local_descriptions.append(offer)
        return offer

    async def create_answer(self) -> SessionDescription:
        self.signaling_state = SignalingState.STABLE
        answer = SessionDescription(DescriptionType.ANSWER, sdp(ufrag="local"))
        self.local_descriptions.append(answer)
        return answer

    async def add_ice_candidate(self, candidate: IceCandidate | None) -> None:
        if self.reject_candidates:
            raise ValueError("bad candidate")
        self.candidates.append(candidate)

    def add_transceiver(self, kind: MediaKind, direction: TransceiverDirection) -> None:
        self.transceivers.append((kind, direction))

    def add_track(self, track: Any) -> FakeSender:
        sender = FakeSender(self.engine.track_kind(track), track)
        self.senders.append(sender)
        self.dirty = True
        return sender

    async def remove_track(self, sender: Any) -> None:
        self.senders.remove(sender)
        self.dirty = True

    def create_data_channel(self, parameters: ChannelParameters) -> FakeChannel:
        channel = FakeChannel(parameters.label, parameters.id)
        self.channels[parameters.label] = channel
        return channel

    def needs_negotiation(self) -> bool:
        return self.dirty

    async def stats(self) -> ConnectionStats:
        return ConnectionStats(
            connection_state=self.connection_state,
            inbound=tuple(
                MediaStats(kind=kind, packets=packets)
                for kind, packets in self.received_packets.items()
            ),
        )

    async def close(self) -> None:
        self.closed = True
        self.signaling_state = SignalingState.CLOSED

    # Test controls -------------------------------------------------------

    def set_state(self, state: ConnectionState) -> None:
        self.connection_state = state
        self.observer.connection_state(state)

    def connect(self) -> None:
        """Connect the transport and open every channel."""
        self.set_state(ConnectionState.CONNECTED)
        for channel in self.channels.values():
            channel.open()

    def remote_channel(self, label: str) -> FakeChannel:
        channel = FakeChannel(label)
        self.observer.data_channel(channel)
        return channel


@dataclass
class FakeEngine:
    capabilities: EngineCapabilities = field(
        default_factory=lambda: EngineCapabilities(
            trickle_ice=True, ice_restart=False, renegotiation=True
        )
    )
    fail_create: bool = False
    peers: list[FakePeer] = field(default_factory=list)
    created: asyncio.Event = field(default_factory=asyncio.Event)

    def track_kind(self, track: Any) -> MediaKind:
        if isinstance(track, NativeTrack | FakeTrack):
            return track.kind
        raise TypeError("not a track")

    async def create_peer(self, config: PeerConfig, observer: PeerObserver) -> FakePeer:
        if self.fail_create:
            raise RuntimeError("no peer")
        peer = FakePeer(self, config, observer)
        self.peers.append(peer)
        self.created.set()
        return peer

    @property
    def peer(self) -> FakePeer:
        assert self.peers, "no peer was created"
        return self.peers[-1]
