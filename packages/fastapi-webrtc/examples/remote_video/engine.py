"""Adapt an existing remote video peer without owning its shared source."""

from typing import Any, Protocol

from fastapi_canon.integrations.webrtc import (
    ConnectionStats,
    EngineCapabilities,
    ErrorCode,
    WebRtcError,
)
from fastapi_canon.integrations.webrtc.engine import (
    ChannelParameters,
    ConnectionState,
    DescriptionType,
    EngineDataChannel,
    EngineSender,
    IceCandidate,
    MediaKind,
    PeerConfig,
    PeerObserver,
    SessionDescription,
    SignalingState,
    TransceiverDirection,
)


class VideoLease(Protocol):
    """One viewer's subscription, never a handle to stop a shared capture process.

    Implementations close idempotently and marshal observer calls onto the event
    loop that called open(). Native connection events determine connection_state;
    receiving an SDP answer alone never means the transport is connected.
    """

    @property
    def signaling_state(self) -> SignalingState: ...

    @property
    def connection_state(self) -> ConnectionState: ...

    async def open(self, config: PeerConfig, observer: PeerObserver) -> None: ...

    async def offer(self) -> SessionDescription: ...

    async def answer(self, description: SessionDescription) -> None: ...

    async def candidate(self, candidate: IceCandidate | None) -> None: ...

    async def aclose(self) -> None: ...


class RemoteVideoEngine:
    """One session's remote peer binding; no native tracks are needed as context."""

    capabilities = EngineCapabilities(
        trickle_ice=True,
        ice_restart=False,
        renegotiation=False,
        negotiated_data_channels=False,
    )

    def __init__(self, lease: VideoLease) -> None:
        self.lease = lease

    def track_kind(self, track: Any) -> MediaKind:
        raise TypeError("Remote video is owned by the viewer lease")

    async def create_peer(
        self, config: PeerConfig, observer: PeerObserver
    ) -> "RemoteVideoPeer":
        await self.lease.open(config, observer)
        return RemoteVideoPeer(self.lease)


class RemoteVideoPeer:
    def __init__(self, lease: VideoLease) -> None:
        self.lease = lease

    @property
    def signaling_state(self) -> SignalingState:
        return self.lease.signaling_state

    @property
    def connection_state(self) -> ConnectionState:
        return self.lease.connection_state

    async def set_remote_description(self, description: SessionDescription) -> None:
        if description.type != DescriptionType.ANSWER:
            raise WebRtcError(
                ErrorCode.NEGOTIATION_FAILED, "This peer only creates offers."
            )
        await self.lease.answer(description)

    async def create_offer(self, *, ice_restart: bool = False) -> SessionDescription:
        if ice_restart:
            raise WebRtcError(
                ErrorCode.NEGOTIATION_FAILED, "ICE restart is unsupported."
            )
        return await self.lease.offer()

    async def create_answer(self) -> SessionDescription:
        raise WebRtcError(
            ErrorCode.NEGOTIATION_FAILED, "This peer only creates offers."
        )

    async def add_ice_candidate(self, candidate: IceCandidate | None) -> None:
        await self.lease.candidate(candidate)

    def add_transceiver(self, kind: MediaKind, direction: TransceiverDirection) -> None:
        raise WebRtcError(ErrorCode.MEDIA_REJECTED)

    def add_track(self, track: Any) -> EngineSender:
        raise WebRtcError(ErrorCode.MEDIA_REJECTED)

    async def remove_track(self, sender: EngineSender) -> None:
        raise WebRtcError(ErrorCode.MEDIA_REJECTED)

    def create_data_channel(self, parameters: ChannelParameters) -> EngineDataChannel:
        raise WebRtcError(ErrorCode.CHANNEL_REJECTED)

    def needs_negotiation(self) -> bool:
        return False

    async def stats(self) -> ConnectionStats:
        # Empty RTP collections mean unavailable observations, not zero traffic.
        return ConnectionStats(connection_state=self.connection_state)

    async def close(self) -> None:
        await self.lease.aclose()
