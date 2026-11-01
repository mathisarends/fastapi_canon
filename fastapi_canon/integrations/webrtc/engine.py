"""The contract between the session runtime and a WebRTC engine adapter.

An engine owns the actual peer connection: SDP, ICE, DTLS, SRTP, SCTP and media.
The runtime owns signaling, admission, limits, deadlines and cleanup, and never
interprets the engine's protocol state beyond the values exposed here.

Adapters must invoke :class:`PeerObserver` callbacks on the event loop that
called :meth:`Engine.create_peer`. Engines with their own threads (for example
GStreamer) marshal callbacks with ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from fastapi_canon.integrations.webrtc.ice import IceServer


class MediaKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"


class SignalingState(StrEnum):
    STABLE = "stable"
    HAVE_LOCAL_OFFER = "have-local-offer"
    HAVE_REMOTE_OFFER = "have-remote-offer"
    CLOSED = "closed"


class ConnectionState(StrEnum):
    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    CLOSED = "closed"


class TransceiverDirection(StrEnum):
    SENDRECV = "sendrecv"
    SENDONLY = "sendonly"
    RECVONLY = "recvonly"
    INACTIVE = "inactive"


class ChannelState(StrEnum):
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class DescriptionType(StrEnum):
    OFFER = "offer"
    ANSWER = "answer"


@dataclass(frozen=True, kw_only=True)
class EngineCapabilities:
    """Optional engine behavior, advertised to clients in the session hello."""

    trickle_ice: bool
    """Local candidates are emitted incrementally instead of inside the SDP."""
    ice_restart: bool
    renegotiation: bool
    """Tracks can be added after the initial negotiation."""
    negotiated_data_channels: bool = True


@dataclass(frozen=True)
class SessionDescription:
    type: DescriptionType
    sdp: str


@dataclass(frozen=True)
class IceCandidate:
    """A trickled candidate in the browser's ``RTCIceCandidateInit`` shape."""

    candidate: str
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None
    username_fragment: str | None = None


@dataclass(frozen=True)
class ChannelParameters:
    label: str
    _: KW_ONLY
    ordered: bool = True
    max_retransmits: int | None = None
    max_packet_life_time_ms: int | None = None
    protocol: str = ""
    negotiated: bool = False
    id: int | None = None


@dataclass(frozen=True)
class PeerConfig:
    ice_servers: tuple[IceServer, ...] = ()


@dataclass(frozen=True)
class CandidatePairStats:
    local_type: str | None = None
    remote_type: str | None = None
    protocol: str | None = None

    @property
    def relayed(self) -> bool:
        return "relay" in (self.local_type, self.remote_type)


@dataclass(frozen=True)
class MediaStats:
    """Per-stream RTP statistics in one direction."""

    kind: MediaKind
    packets: int
    bytes: int | None = None
    packets_lost: int | None = None
    jitter_seconds: float | None = None


@dataclass(frozen=True)
class ConnectionStats:
    """Engine-neutral connection statistics; unsupported values are ``None``.

    Empty inbound/outbound tuples mean no stream observations were supplied,
    not necessarily zero traffic. ``received()`` sums only supplied observations.
    """

    connection_state: ConnectionState
    selected_candidate_pair: CandidatePairStats | None = None
    round_trip_time_seconds: float | None = None
    inbound: tuple[MediaStats, ...] = ()
    outbound: tuple[MediaStats, ...] = ()

    def received(self, kind: MediaKind) -> int:
        """Packets received for ``kind`` across all inbound streams."""
        return sum(s.packets for s in self.inbound if s.kind == kind)


@runtime_checkable
class EngineTrack(Protocol):
    """A remote media track delivered by the engine."""

    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> MediaKind: ...

    @property
    def native(self) -> Any:
        """The engine's own track object, for engine-specific processing."""
        ...

    async def recv(self) -> Any:
        """Receive the next decoded frame in the engine's native frame type."""
        ...

    def on_ended(self, callback: Callable[[], None]) -> None: ...

    def stop(self) -> None: ...


class EngineSender(Protocol):
    """Handle for a local track attached to a peer."""

    @property
    def kind(self) -> MediaKind: ...


class EngineDataChannel(Protocol):
    @property
    def label(self) -> str: ...

    @property
    def id(self) -> int | None: ...

    @property
    def ready_state(self) -> ChannelState: ...

    @property
    def buffered_amount(self) -> int: ...

    def set_buffered_amount_low_threshold(self, value: int) -> None: ...

    def observe(
        self,
        *,
        on_open: Callable[[], None],
        on_message: Callable[[str | bytes], None],
        on_close: Callable[[], None],
        on_buffered_amount_low: Callable[[], None],
    ) -> None:
        """Register callbacks. Only one observer is supported per channel."""
        ...

    def send(self, data: str | bytes) -> None: ...

    def close(self) -> None: ...


class PeerObserver(Protocol):
    def ice_candidate(self, candidate: IceCandidate | None) -> None:
        """A local candidate, or ``None`` when local gathering completed."""
        ...

    def connection_state(self, state: ConnectionState) -> None: ...

    def track(self, track: EngineTrack) -> None: ...

    def data_channel(self, channel: EngineDataChannel) -> None:
        """A channel opened in-band by the remote peer."""
        ...


class EnginePeer(Protocol):
    @property
    def signaling_state(self) -> SignalingState: ...

    @property
    def connection_state(self) -> ConnectionState: ...

    async def set_remote_description(self, description: SessionDescription) -> None: ...

    async def create_offer(self, *, ice_restart: bool = False) -> SessionDescription:
        """Create an offer and apply it as the local description."""
        ...

    async def create_answer(self) -> SessionDescription:
        """Create an answer and apply it as the local description."""
        ...

    async def add_ice_candidate(self, candidate: IceCandidate | None) -> None:
        """Add a remote candidate; ``None`` signals end-of-candidates."""
        ...

    def add_transceiver(
        self, kind: MediaKind, direction: TransceiverDirection
    ) -> None: ...

    def add_track(self, track: Any) -> EngineSender:
        """Attach a native local track (the adapter defines accepted types)."""
        ...

    async def remove_track(self, sender: EngineSender) -> None: ...

    def create_data_channel(
        self, parameters: ChannelParameters
    ) -> EngineDataChannel: ...

    def needs_negotiation(self) -> bool:
        """Whether local changes are not yet reflected in a negotiated description."""
        ...

    async def stats(self) -> ConnectionStats: ...

    async def close(self) -> None:
        """Release every engine resource. Must be idempotent."""
        ...


class Engine(Protocol):
    @property
    def capabilities(self) -> EngineCapabilities: ...

    def track_kind(self, track: Any) -> MediaKind:
        """Return the kind of a native local track, or raise ``TypeError``."""
        ...

    async def create_peer(
        self, config: PeerConfig, observer: PeerObserver
    ) -> EnginePeer: ...
