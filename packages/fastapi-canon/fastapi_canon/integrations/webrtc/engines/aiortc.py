"""Reference engine backed by `aiortc <https://github.com/aiortc/aiortc>`_.

Install with ``pip install "fastapi-canon[webrtc,aiortc]"``. Local tracks are aiortc
``MediaStreamTrack`` instances (for example from ``aiortc.contrib.media``) and
incoming frames are PyAV ``AudioFrame``/``VideoFrame`` objects.

aiortc gathers all local candidates before it returns a description, so the
adapter advertises ``trickle_ice=False`` and emits end-of-candidates right after
each local description. It cannot restart ICE or roll back descriptions; the
browser client therefore never requests ICE restarts from this engine and the
server stays the impolite peer during simultaneous offers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import Any, cast

try:
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCDataChannel,
        RTCIceServer,
        RTCPeerConnection,
        RTCRtpSender,
        RTCRtpTransceiver,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError
    from aiortc.sdp import SessionDescription as SdpDocument
    from aiortc.sdp import candidate_from_sdp
    from aiortc.stats import (
        RTCInboundRtpStreamStats,
        RTCOutboundRtpStreamStats,
        RTCRemoteInboundRtpStreamStats,
    )
except ImportError as error:  # pragma: no cover - exercised without the extra
    raise ImportError(
        "The aiortc engine requires the 'aiortc' extra: "
        'pip install "fastapi-canon[webrtc,aiortc]"'
    ) from error

from fastapi_canon.integrations.webrtc.engine import (
    CandidatePairStats,
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
from fastapi_canon.integrations.webrtc.ice import IceServer

logger = logging.getLogger("fastapi_canon.integrations.webrtc.aiortc")

_CLOSE_GRACE_SECONDS = 0.5

_CAPABILITIES = EngineCapabilities(
    trickle_ice=False,
    ice_restart=False,
    renegotiation=True,
    negotiated_data_channels=True,
)


class AiortcEngine:
    """Create aiortc peer connections for sessions.

    ``codec_preferences`` optionally maps a kind to preferred MIME types in
    order, for example ``{MediaKind.VIDEO: ("video/VP8",)}``.
    """

    def __init__(
        self, *, codec_preferences: dict[MediaKind, Sequence[str]] | None = None
    ) -> None:
        self._codec_preferences = dict(codec_preferences or {})

    @property
    def capabilities(self) -> EngineCapabilities:
        return _CAPABILITIES

    def track_kind(self, track: Any) -> MediaKind:
        if not isinstance(track, MediaStreamTrack) or track.kind not in (
            "audio",
            "video",
        ):
            raise TypeError("The aiortc engine sends aiortc MediaStreamTrack values")
        return MediaKind(track.kind)

    async def create_peer(
        self, config: PeerConfig, observer: PeerObserver
    ) -> AiortcPeer:
        connection = RTCPeerConnection(
            RTCConfiguration(iceServers=[_ice_server(s) for s in config.ice_servers])
        )
        return AiortcPeer(connection, observer, self._codec_preferences)


def _ice_server(server: IceServer) -> RTCIceServer:
    return RTCIceServer(
        urls=list(server.urls),
        username=server.username,
        credential=server.credential,
    )


class AiortcPeer:
    def __init__(
        self,
        connection: RTCPeerConnection,
        observer: PeerObserver,
        codec_preferences: dict[MediaKind, Sequence[str]],
    ) -> None:
        self.connection = connection
        self._observer = observer
        self._codec_preferences = codec_preferences
        self._dirty = False
        self._closed = False
        connection.on("connectionstatechange", self._state_changed)
        connection.on("track", self._track)
        connection.on("datachannel", self._datachannel)

    @property
    def signaling_state(self) -> SignalingState:
        return SignalingState(self.connection.signalingState)

    @property
    def connection_state(self) -> ConnectionState:
        return ConnectionState(self.connection.connectionState)

    def _state_changed(self) -> None:
        self._observer.connection_state(self.connection_state)

    def _track(self, track: MediaStreamTrack) -> None:
        self._observer.track(AiortcTrack(track))

    def _datachannel(self, channel: RTCDataChannel) -> None:
        self._observer.data_channel(AiortcDataChannel(channel))

    async def set_remote_description(self, description: SessionDescription) -> None:
        await self.connection.setRemoteDescription(
            RTCSessionDescription(sdp=description.sdp, type=description.type)
        )

    async def create_offer(self, *, ice_restart: bool = False) -> SessionDescription:
        if ice_restart:
            raise NotImplementedError("aiortc cannot restart ICE")
        self._apply_codec_preferences()
        negotiated = self._negotiated_parameters()
        offer = await self.connection.createOffer()
        if negotiated:
            offer = _keep_negotiated(offer, negotiated)
            for transceiver in self.connection.getTransceivers():
                saved = negotiated.get(transceiver.mid or "")
                if saved is not None:
                    transceiver._codecs, transceiver._headerExtensions = saved
        return await self._apply_local(offer)

    def _negotiated_parameters(self) -> dict[str, tuple[list[Any], list[Any]]]:
        # aiortc's createOffer resets every transceiver to its default payload
        # types and header-extension IDs. Browsers reject re-offers that remap
        # them on an established m-line, so re-offers keep the negotiated ones.
        return {
            transceiver.mid: (
                list(transceiver._codecs),
                list(transceiver._headerExtensions),
            )
            for transceiver in self.connection.getTransceivers()
            if transceiver.mid is not None
            and transceiver.currentDirection is not None
            and transceiver._codecs
        }

    async def create_answer(self) -> SessionDescription:
        self._apply_codec_preferences()
        answer = await self.connection.createAnswer()
        return await self._apply_local(answer)

    async def _apply_local(
        self, description: RTCSessionDescription
    ) -> SessionDescription:
        await self.connection.setLocalDescription(description)
        self._dirty = False
        local = self.connection.localDescription
        # aiortc embeds every gathered candidate in the description.
        self._observer.ice_candidate(None)
        return SessionDescription(type=DescriptionType(local.type), sdp=local.sdp)

    async def add_ice_candidate(self, candidate: IceCandidate | None) -> None:
        if candidate is None:
            await self.connection.addIceCandidate(None)
            return
        value = candidate.candidate.removeprefix("a=").removeprefix("candidate:")
        parsed = candidate_from_sdp(value)
        parsed.sdpMid = candidate.sdp_mid
        parsed.sdpMLineIndex = candidate.sdp_mline_index
        await self.connection.addIceCandidate(parsed)

    def add_transceiver(self, kind: MediaKind, direction: TransceiverDirection) -> None:
        self.connection.addTransceiver(kind, direction=direction)
        self._dirty = True

    def add_track(self, track: Any) -> AiortcSender:
        # RTP senders stop their track on close. Source ownership belongs to
        # SessionRuntime, so the engine only owns this forwarding track.
        sender = self.connection.addTrack(_SenderTrack(track))
        self._dirty = True
        return AiortcSender(sender, MediaKind(track.kind))

    async def remove_track(self, sender: Any) -> None:
        native = cast(AiortcSender, sender).native
        for transceiver in self.connection.getTransceivers():
            if transceiver.sender is native:
                if native.track is not None:
                    native.track.stop()
                native.replaceTrack(None)
                transceiver.direction = (
                    "recvonly"
                    if transceiver.direction in ("sendrecv", "recvonly")
                    else "inactive"
                )
                self._dirty = True
                return

    def create_data_channel(self, parameters: ChannelParameters) -> AiortcDataChannel:
        channel = self.connection.createDataChannel(
            parameters.label,
            maxPacketLifeTime=parameters.max_packet_life_time_ms,
            maxRetransmits=parameters.max_retransmits,
            ordered=parameters.ordered,
            protocol=parameters.protocol,
            negotiated=parameters.negotiated,
            id=parameters.id,
        )
        return AiortcDataChannel(channel)

    def needs_negotiation(self) -> bool:
        return self._dirty or any(
            t.mid is None and not t.stopped for t in self.connection.getTransceivers()
        )

    async def stats(self) -> ConnectionStats:
        report = await self.connection.getStats()
        inbound: list[MediaStats] = []
        outbound: list[MediaStats] = []
        round_trip: float | None = None
        for item in report.values():
            if isinstance(item, RTCInboundRtpStreamStats):
                inbound.append(
                    MediaStats(
                        kind=MediaKind(item.kind),
                        packets=item.packetsReceived,
                        packets_lost=item.packetsLost,
                        # aiortc reports RTP timestamp units, but exposes no
                        # codec/clock-rate link for this stream in its stats.
                        jitter_seconds=None,
                    )
                )
            elif isinstance(item, RTCOutboundRtpStreamStats):
                outbound.append(
                    MediaStats(
                        kind=MediaKind(item.kind),
                        packets=item.packetsSent,
                        bytes=item.bytesSent,
                    )
                )
            elif isinstance(item, RTCRemoteInboundRtpStreamStats):
                round_trip = item.roundTripTime
        return ConnectionStats(
            connection_state=self.connection_state,
            selected_candidate_pair=self._selected_pair(),
            round_trip_time_seconds=round_trip,
            inbound=tuple(inbound),
            outbound=tuple(outbound),
        )

    def _selected_pair(self) -> CandidatePairStats | None:
        # aiortc has no public selected-pair API; read aioice best-effort.
        transports: list[Any] = [
            t.receiver.transport for t in self.connection.getTransceivers()
        ]
        if self.connection.sctp is not None:
            transports.append(self.connection.sctp.transport)
        for dtls in transports:
            with contextlib.suppress(Exception):
                nominated = dtls.transport._connection._nominated
                for pair in nominated.values():
                    local = pair.local_candidate
                    remote = pair.remote_candidate
                    return CandidatePairStats(
                        local_type=local.type,
                        remote_type=remote.type,
                        protocol=local.transport,
                    )
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closing = asyncio.ensure_future(self.connection.close())
        try:
            done, _ = await asyncio.wait({closing}, timeout=_CLOSE_GRACE_SECONDS)
            if not done:
                self._abort_closing_sockets()
            await closing
        except BaseException:
            closing.cancel()
            raise

    def _abort_closing_sockets(self) -> None:
        # Windows' proactor loop never reports a closed datagram transport while
        # a send is in flight, which makes aioice wait forever. Aborting a
        # transport that is already closing reports it as closed immediately.
        transports: list[Any] = [
            t.receiver.transport.transport for t in self.connection.getTransceivers()
        ]
        if self.connection.sctp is not None:
            transports.append(self.connection.sctp.transport.transport)
        for ice in transports:
            with contextlib.suppress(Exception):
                for protocol in ice._connection._protocols:
                    transport = protocol.transport
                    if transport is not None and transport.is_closing():
                        transport.abort()

    def _apply_codec_preferences(self) -> None:
        if not self._codec_preferences:
            return
        for transceiver in self.connection.getTransceivers():
            preferred = self._codec_preferences.get(MediaKind(transceiver.kind))
            if preferred:
                _prefer(transceiver, preferred)


def _keep_negotiated(
    offer: RTCSessionDescription,
    negotiated: dict[str, tuple[list[Any], list[Any]]],
) -> RTCSessionDescription:
    document = SdpDocument.parse(offer.sdp)
    for media in document.media:
        saved = negotiated.get(media.rtp.muxId)
        if saved is None:
            continue
        codecs, extensions = saved
        media.rtp.codecs = codecs
        media.rtp.headerExtensions = extensions
        media.fmt = [codec.payloadType for codec in codecs]
    return RTCSessionDescription(sdp=str(document), type=offer.type)


def _prefer(transceiver: RTCRtpTransceiver, mime_types: Sequence[str]) -> None:
    capabilities = RTCRtpSender.getCapabilities(transceiver.kind)
    order = {mime.lower(): index for index, mime in enumerate(mime_types)}
    codecs = sorted(
        (c for c in capabilities.codecs if c.mimeType.lower() in order),
        key=lambda c: order[c.mimeType.lower()],
    )
    if codecs:
        transceiver.setCodecPreferences(codecs)


class _SenderTrack(MediaStreamTrack):
    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self._source = source
        self.kind = source.kind
        self._id = source.id

    async def recv(self) -> Any:
        if self.readyState != "live":
            raise MediaStreamError
        return await self._source.recv()


class AiortcSender:
    def __init__(self, native: RTCRtpSender, kind: MediaKind) -> None:
        self.native = native
        self._kind = kind

    @property
    def kind(self) -> MediaKind:
        return self._kind


class AiortcTrack:
    def __init__(self, track: MediaStreamTrack) -> None:
        self._track = track

    @property
    def id(self) -> str:
        return str(self._track.id)

    @property
    def kind(self) -> MediaKind:
        return MediaKind(self._track.kind)

    @property
    def native(self) -> MediaStreamTrack:
        return self._track

    async def recv(self) -> Any:
        return await self._track.recv()

    def on_ended(self, callback: Callable[[], None]) -> None:
        if self._track.readyState == "ended":
            callback()
            return
        self._track.on("ended", callback)

    def stop(self) -> None:
        self._track.stop()


class AiortcDataChannel:
    def __init__(self, channel: RTCDataChannel) -> None:
        self.native = channel

    @property
    def label(self) -> str:
        return str(self.native.label)

    @property
    def id(self) -> int | None:
        return self.native.id

    @property
    def ready_state(self) -> ChannelState:
        return ChannelState(self.native.readyState)

    @property
    def buffered_amount(self) -> int:
        return int(self.native.bufferedAmount)

    def set_buffered_amount_low_threshold(self, value: int) -> None:
        self.native.bufferedAmountLowThreshold = value

    def observe(
        self,
        *,
        on_open: Callable[[], None],
        on_message: Callable[[str | bytes], None],
        on_close: Callable[[], None],
        on_buffered_amount_low: Callable[[], None],
    ) -> None:
        self.native.on("open", on_open)
        self.native.on("message", on_message)
        self.native.on("close", on_close)
        self.native.on("bufferedamountlow", on_buffered_amount_low)

    def send(self, data: str | bytes) -> None:
        self.native.send(data)

    def close(self) -> None:
        self.native.close()
