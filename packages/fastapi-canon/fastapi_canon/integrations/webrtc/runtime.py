"""The per-session state machine around signaling, limits and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anyio import CancelScope
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from fastapi_canon.integrations.webrtc import protocol
from fastapi_canon.integrations.webrtc.declarations import (
    DataChannelSpec,
    Direction,
    MessageFormat,
    Role,
    Session,
    SessionContext,
    SessionDeclaration,
)
from fastapi_canon.integrations.webrtc.engine import (
    ChannelParameters,
    ChannelState,
    ConnectionState,
    ConnectionStats,
    DescriptionType,
    Engine,
    EngineDataChannel,
    EnginePeer,
    EngineTrack,
    IceCandidate,
    MediaKind,
    PeerConfig,
    SessionDescription,
    SignalingState,
    TransceiverDirection,
)
from fastapi_canon.integrations.webrtc.errors import (
    ErrorCode,
    SessionClosedError,
    WebRtcError,
    close_code,
    default_message,
)
from fastapi_canon.integrations.webrtc.events import (
    EventHook,
    SessionEvent,
    SessionEventKind,
)
from fastapi_canon.integrations.webrtc.ice import (
    IceServers,
    IceServerSource,
    resolve_ice_servers,
)
from fastapi_canon.integrations.webrtc.peer import IncomingTrack, OutgoingTrack, Peer
from fastapi_canon.integrations.webrtc.sdp import DescriptionSummary, summarize

if TYPE_CHECKING:
    from fastapi_canon.integrations.webrtc.router import _Slots

logger = logging.getLogger("fastapi_canon.integrations.webrtc")

_NORMAL_CLOSES = frozenset({1000, 1001})
_RESTART_CLOSE = 1012
_MEDIA_POLL_SECONDS = 0.2
MINIMUM_CLEANUP_STEP_SECONDS = 1.0
"""Budget ``on_close`` and engine cleanup keep after stuck tasks used the deadline."""


@dataclass
class _Channel:
    spec: DataChannelSpec
    max_bytes: int
    queue: asyncio.Queue[Any]
    engine: EngineDataChannel | None = None
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    consumer: bool = False

    @property
    def is_open(self) -> bool:
        return self.opened.is_set() and not self.closed.is_set()


@dataclass(frozen=True)
class RuntimeOptions:
    engine: Engine
    ice_servers: IceServerSource | None
    on_event: EventHook | None


class SessionRuntime:
    """Owns one admitted session from WebSocket accept until cleanup finished."""

    def __init__(
        self,
        *,
        declaration: SessionDeclaration,
        options: RuntimeOptions,
        websocket: WebSocket,
        session: Session,
        session_id: str,
        slots: _Slots,
    ) -> None:
        self.declaration = declaration
        self.session_id = session_id
        self.code: ErrorCode | None = None
        self.incoming: list[IncomingTrack] = []
        self._options = options
        self._engine = options.engine
        self._limits = declaration.limits
        self._ws = websocket
        self._session = session
        self._slots = slots
        self._peer_handle = Peer(self)
        self._peer: EnginePeer | None = None
        self._admitted_at = time.monotonic()
        self._done = asyncio.Event()
        self._finished = asyncio.Event()
        self._started = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._outbound: asyncio.Queue[protocol.ServerMessage] = asyncio.Queue(
            maxsize=self._limits.max_outbound_signals
        )
        self._negotiation = asyncio.Lock()
        self._held_candidates: list[IceCandidate | None] | None = None
        self._pending_candidates: list[IceCandidate | None] = []
        self._remote_applied = False
        self._remote_ufrags: frozenset[str] = frozenset()
        self._ignored_offer = False
        self._negotiations = 0
        self._local_offer: int | None = None
        self._offer_settled: asyncio.Event | None = None
        self._initial_tracks_attached = False
        self._connection_state = ConnectionState.NEW
        self._ever_connected = False
        self._established = False
        self._signaling_open = False
        self._signaling_lost = False
        self._accepted = False
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._queued_tracks: list[IncomingTrack] = []
        self._incoming_counts: dict[str, int] = {"audio": 0, "video": 0}
        self._outgoing: list[OutgoingTrack] = []
        # Session.tracks transfer ownership before negotiation can attach them.
        self._owned_tracks = {id(track): track for track in session.tracks}
        self._channels = {
            spec.name: _Channel(
                spec=spec,
                max_bytes=declaration.max_message_bytes(spec),
                queue=asyncio.Queue(maxsize=self._limits.max_queued_messages),
            )
            for spec in declaration.channels
        }
        self._context = SessionContext(
            name=declaration.name, session_id=session_id, websocket=websocket
        )

    # ------------------------------------------------------------------ public

    async def serve(self) -> None:
        """Run the session to completion. Never raises except for cancellation."""
        cancelled = False
        try:
            if await self._accept():
                await self._run()
        except asyncio.CancelledError:
            cancelled = True
            self.terminate(ErrorCode.SHUTDOWN)
        except WebRtcError as error:
            self.terminate(error.code)
        except Exception:
            logger.exception("Session %s failed unexpectedly", self.declaration.name)
            self.terminate(ErrorCode.ENGINE_FAILURE)
        finally:
            # ASGI servers may cancel the endpoint while cleanup is awaiting I/O.
            # Do not let dependency scopes exit while the shielded task runs on.
            cleanup = asyncio.create_task(self._cleanup_once())
            with CancelScope(shield=True):
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    def terminate(self, code: ErrorCode) -> None:
        if self.code is None:
            self.code = code
            self._done.set()

    async def wait_closed(self) -> ErrorCode:
        await self._done.wait()
        assert self.code is not None
        return self.code

    async def wait_finished(self) -> None:
        await self._finished.wait()

    def channel_state(self, channel: DataChannelSpec[Any, Any] | str) -> _Channel:
        name = channel.name if isinstance(channel, DataChannelSpec) else channel
        state = self._channels.get(name)
        if state is None:
            raise ValueError(
                f"Session {self.declaration.name!r} has no channel {name!r}"
            )
        if isinstance(channel, DataChannelSpec) and channel != state.spec:
            raise ValueError(
                f"Channel {name!r} of session {self.declaration.name!r} is "
                "declared with a different DataChannelSpec"
            )
        return state

    async def send_message(
        self, target: DataChannelSpec[Any, Any] | str, message: Any
    ) -> None:
        channel = self.channel_state(target)
        data = _encode(channel.spec, message)
        size = len(data.encode()) if isinstance(data, str) else len(data)
        if size > channel.max_bytes:
            raise WebRtcError(ErrorCode.MESSAGE_TOO_LARGE)
        async with channel.send_lock:
            await self._until(channel.opened)
            if channel.closed.is_set() or channel.engine is None:
                raise WebRtcError(ErrorCode.CHANNEL_CLOSED)
            engine = channel.engine
            high = self._limits.send_buffer_high_bytes
            if engine.buffered_amount > high:
                deadline = self._limits.send_timeout_seconds
                try:
                    async with asyncio.timeout(deadline):
                        while engine.buffered_amount > high:
                            channel.drained.clear()
                            await self._until(channel.drained, channel.closed)
                            if channel.closed.is_set():
                                raise WebRtcError(ErrorCode.CHANNEL_CLOSED)
                except TimeoutError:
                    self.terminate(ErrorCode.SLOW_CONSUMER)
                    raise SessionClosedError(ErrorCode.SLOW_CONSUMER) from None
            self._ensure_active()
            try:
                engine.send(data)
            except Exception as error:
                if channel.closed.is_set():
                    raise WebRtcError(ErrorCode.CHANNEL_CLOSED) from error
                raise WebRtcError(ErrorCode.ENGINE_FAILURE) from error

    async def next_message(self, channel: _Channel) -> Any:
        """Return the next received message; raise ``StopAsyncIteration`` once the
        session or channel closed and every queued message was consumed."""
        if channel.spec.name in self._session.message_handlers:
            raise RuntimeError(
                f"Channel {channel.spec.name!r} is consumed by its on_message handler"
            )
        if channel.spec.receive_format == MessageFormat.NONE:
            raise RuntimeError(
                f"Channel {channel.spec.name!r} does not receive messages"
            )
        if channel.consumer:
            raise RuntimeError(f"Channel {channel.spec.name!r} already has a reader")
        channel.consumer = True
        try:
            while True:
                if not channel.queue.empty():
                    return channel.queue.get_nowait()
                if self.code is not None or channel.closed.is_set():
                    raise StopAsyncIteration
                getter = asyncio.ensure_future(channel.queue.get())
                stop = asyncio.ensure_future(self._first(self._done, channel.closed))
                try:
                    await asyncio.wait(
                        {getter, stop}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    stop.cancel()
                    if not getter.done():
                        getter.cancel()
                if getter.done() and not getter.cancelled():
                    return getter.result()
        finally:
            channel.consumer = False

    async def send_track(self, native: Any, *, owned: bool) -> OutgoingTrack:
        self._ensure_active()
        try:
            kind = self._engine.track_kind(native)
        except TypeError as error:
            raise WebRtcError(
                ErrorCode.MEDIA_REJECTED, "The engine does not accept this track."
            ) from error
        direction = self.declaration.media.direction(kind)
        if direction is None or not direction.sends:
            raise WebRtcError(
                ErrorCode.MEDIA_REJECTED,
                f"The session does not send {kind} media.",
            )
        handle = OutgoingTrack(self, native, kind, owned)
        async with self._negotiation:
            self._ensure_active()
            peer = self._require_peer()
            if owned:
                self._owned_tracks[id(native)] = native
            handle._sender = self._engine_call(lambda: peer.add_track(native))
            self._outgoing.append(handle)
        if self._remote_applied or self.declaration.role is Role.OFFERER:
            await self._renegotiate(wait=True)
        return handle

    async def remove_track(self, handle: OutgoingTrack) -> None:
        if handle.removed:
            return
        handle.removed = True
        sender = handle._sender
        if sender is not None and self._peer is not None and self.code is None:
            async with self._negotiation:
                peer = self._require_peer()
                try:
                    await peer.remove_track(sender)
                except Exception as error:
                    raise WebRtcError(ErrorCode.ENGINE_FAILURE) from error
            if handle.owned:
                self._release_track(handle.native)
            await self._renegotiate(wait=True)
        elif handle.owned:
            self._release_track(handle.native)

    def _release_track(self, native: Any) -> None:
        if self._owned_tracks.pop(id(native), None) is not None:
            _stop_native(native)

    async def stats(self) -> ConnectionStats:
        peer = self._require_peer()
        try:
            return await peer.stats()
        except Exception as error:
            raise WebRtcError(ErrorCode.ENGINE_FAILURE) from error

    # --------------------------------------------------------------- startup

    async def _accept(self) -> bool:
        offered = self._ws.scope.get("subprotocols") or []
        if offered and protocol.SUBPROTOCOL not in offered:
            await self._ws.accept()
            self._signaling_open = True
            self.terminate(ErrorCode.UNSUPPORTED_PROTOCOL)
            return False
        subprotocol = protocol.SUBPROTOCOL if offered else None
        await self._ws.accept(subprotocol=subprotocol)
        self._signaling_open = True
        self._accepted = True
        self._emit(SessionEventKind.ADMITTED)
        return True

    async def _run(self) -> None:
        self._timer(
            "negotiation",
            self._limits.negotiation_timeout_seconds,
            ErrorCode.NEGOTIATION_TIMEOUT,
        )
        self._spawn(self._startup(), "startup", ErrorCode.ENGINE_FAILURE)
        await self._done.wait()

    async def _startup(self) -> None:
        source = (
            self._session.ice_servers
            if self._session.ice_servers is not None
            else self._options.ice_servers
        )
        try:
            ice = await resolve_ice_servers(source, self._context)
        except Exception as error:
            logger.exception("ICE server provider failed for %s", self._label)
            raise WebRtcError(ErrorCode.HANDLER_FAILURE) from error
        self._ensure_active()
        await self._create_peer(ice)
        try:
            await self._ws.send_text(protocol.encode(self._hello(ice)))
        except Exception as error:
            raise WebRtcError(ErrorCode.SIGNALING_LOST) from error
        self._spawn(self._read(), "signaling-read", ErrorCode.PROTOCOL_ERROR)
        self._spawn(self._write(), "signaling-write", ErrorCode.SIGNALING_LOST)
        if self.declaration.role is Role.OFFERER:
            self._spawn(self._renegotiate(), "offer", ErrorCode.NEGOTIATION_FAILED)

    async def _create_peer(self, ice: IceServers) -> None:
        try:
            self._peer = await self._engine.create_peer(
                PeerConfig(ice_servers=ice.for_engine), _Observer(self)
            )
        except WebRtcError:
            raise
        except Exception as error:
            logger.exception("Engine could not create a peer for %s", self._label)
            raise WebRtcError(ErrorCode.ENGINE_FAILURE) from error
        peer = self._peer
        if self.code is not None:
            # An adapter may return a peer while handling cancellation. Do not
            # start signaling or leak a peer returned after cleanup's deadline.
            await peer.close()
            self._ensure_active()
        declaration = self.declaration
        for spec in declaration.channels:
            if not declaration.server_creates(spec):
                continue
            parameters = ChannelParameters(
                label=spec.name,
                ordered=spec.ordered,
                max_retransmits=spec.max_retransmits,
                max_packet_life_time_ms=spec.max_packet_life_time_ms,
                negotiated=spec.negotiated,
                id=declaration.channel_ids[spec.name],
            )
            engine_channel = self._engine_call(
                functools.partial(peer.create_data_channel, parameters)
            )
            self._bind_channel(self._channels[spec.name], engine_channel)
        if declaration.role is Role.OFFERER:
            self._attach_initial_tracks()
            media = declaration.media
            for kind in MediaKind:
                direction = media.direction(kind)
                if direction is None or not direction.receives:
                    continue
                if any(t.kind == kind for t in self._outgoing) and (
                    direction is Direction.SEND_RECEIVE
                ):
                    continue
                self._engine_call(
                    functools.partial(
                        peer.add_transceiver, kind, TransceiverDirection.RECVONLY
                    )
                )

    def _attach_initial_tracks(self) -> None:
        if self._initial_tracks_attached:
            return
        self._initial_tracks_attached = True
        peer = self._require_peer()
        for native in self._session.tracks:
            try:
                kind = self._engine.track_kind(native)
            except TypeError as error:
                raise WebRtcError(ErrorCode.MEDIA_REJECTED) from error
            direction = self.declaration.media.direction(kind)
            if direction is None or not direction.sends:
                raise WebRtcError(
                    ErrorCode.MEDIA_REJECTED,
                    f"The session does not send {kind} media.",
                )
            handle = OutgoingTrack(self, native, kind, owned=True)
            handle._sender = self._engine_call(
                functools.partial(peer.add_track, native)
            )
            self._outgoing.append(handle)

    def _hello(self, ice: IceServers) -> protocol.SessionMessage:
        declaration = self.declaration
        media = declaration.media
        capabilities = self._engine.capabilities
        return protocol.SessionMessage(
            session_id=self.session_id,
            name=declaration.name,
            role=declaration.role,
            ice_servers=ice.client,
            media=protocol.MediaDescriptor(
                audio=media.audio,
                video=media.video,
                max_incoming_tracks=media.max_incoming_tracks,
            ),
            channels=tuple(
                protocol.ChannelDescriptor(
                    name=spec.name,
                    id=declaration.channel_ids[spec.name],
                    negotiated=spec.negotiated,
                    ordered=spec.ordered,
                    max_retransmits=spec.max_retransmits,
                    max_packet_life_time_ms=spec.max_packet_life_time_ms,
                    max_message_bytes=declaration.max_message_bytes(spec),
                    receive_format=spec.receive_format,
                    send_format=spec.send_format,
                )
                for spec in declaration.channels
            ),
            limits=protocol.LimitsDescriptor(
                negotiation_timeout_ms=round(
                    self._limits.negotiation_timeout_seconds * 1000
                ),
                max_signaling_message_bytes=self._limits.max_signaling_message_bytes,
                max_pending_candidates=self._limits.max_pending_candidates,
            ),
            capabilities=protocol.CapabilitiesDescriptor(
                trickle_ice=capabilities.trickle_ice,
                ice_restart=capabilities.ice_restart,
                renegotiation=capabilities.renegotiation,
            ),
        )

    # -------------------------------------------------------------- signaling

    async def _read(self) -> None:
        limit = self._limits.max_signaling_message_bytes
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                self._signaling_open = False
                self._on_signaling_closed(int(message.get("code") or 1005))
                return
            text = message.get("text")
            if text is None:
                raise WebRtcError(ErrorCode.PROTOCOL_ERROR)
            if len(text) > limit or len(text.encode()) > limit:
                raise WebRtcError(ErrorCode.MESSAGE_TOO_LARGE)
            try:
                parsed = protocol.client_message_adapter.validate_json(text)
            except ValidationError:
                raise WebRtcError(ErrorCode.PROTOCOL_ERROR) from None
            if isinstance(parsed, protocol.DescriptionMessage):
                await self._on_description(parsed)
            else:
                payload = parsed.candidate
                await self._on_candidate(
                    None if payload is None else payload.to_engine()
                )

    def _on_signaling_closed(self, code: int) -> None:
        if code in _NORMAL_CLOSES:
            self.terminate(ErrorCode.CLOSED)
            return
        if code == _RESTART_CLOSE:
            self.terminate(ErrorCode.SHUTDOWN)
            return
        grace = self._limits.signaling_loss_grace_seconds
        if not self._established or grace <= 0:
            self.terminate(ErrorCode.SIGNALING_LOST)
            return
        logger.info(
            "Signaling for %s was lost; keeping media for %.1fs", self._label, grace
        )
        self._signaling_lost = True
        self._timer("signaling", grace, ErrorCode.SIGNALING_LOST)

    async def _write(self) -> None:
        while True:
            message = await self._outbound.get()
            if self._signaling_lost or not self._signaling_open:
                continue
            try:
                await self._ws.send_text(protocol.encode(message))
            except Exception:
                # The reader observes the disconnect and classifies it.
                self._signaling_open = False

    def _send(self, message: protocol.ServerMessage) -> None:
        if self._signaling_lost or not self._signaling_open or self.code is not None:
            return
        try:
            self._outbound.put_nowait(message)
        except asyncio.QueueFull:
            self.terminate(ErrorCode.QUEUE_EXHAUSTED)

    def _report(self, code: ErrorCode, channel: str | None = None) -> None:
        self._send(
            protocol.ErrorMessage(
                code=code, message=default_message(code), channel=channel
            )
        )

    async def _on_description(self, message: protocol.DescriptionMessage) -> None:
        description = message.description.to_engine()
        if description.type == DescriptionType.OFFER:
            await self._on_offer(description, message.negotiation)
        else:
            await self._on_answer(description, message.negotiation)
        await self._renegotiate_if_needed()

    async def _on_offer(self, offer: SessionDescription, negotiation: int) -> None:
        async with self._negotiation:
            self._ensure_active()
            peer = self._require_peer()
            if peer.signaling_state != SignalingState.STABLE:
                # The server is the impolite peer: it keeps its own offer and the
                # browser rolls back (perfect negotiation).
                logger.debug("Ignoring a colliding offer for %s", self._label)
                self._ignored_offer = True
                return
            self._ignored_offer = False
            summary = summarize(offer.sdp)
            self._check_media(summary)
            renegotiation = self._remote_applied
            await self._negotiate_call(peer.set_remote_description(offer))
            self._remote_description_applied(summary)
            if self.declaration.role is Role.ANSWERER:
                self._attach_initial_tracks()
            answer = await self._describe(peer.create_answer)
            self._send(
                protocol.DescriptionMessage(
                    description=protocol.DescriptionPayload.from_engine(answer),
                    negotiation=negotiation,
                )
            )
            self._release_candidates()
            await self._flush_pending_candidates()
        if renegotiation:
            self._emit(SessionEventKind.RENEGOTIATED)

    async def _on_answer(self, answer: SessionDescription, negotiation: int) -> None:
        async with self._negotiation:
            self._ensure_active()
            peer = self._require_peer()
            if (
                peer.signaling_state != SignalingState.HAVE_LOCAL_OFFER
                or self._local_offer != negotiation
            ):
                logger.debug("Dropping a stale answer for %s", self._label)
                return
            summary = summarize(answer.sdp)
            self._check_media(summary)
            renegotiation = self._remote_applied
            await self._negotiate_call(peer.set_remote_description(answer))
            self._remote_description_applied(summary)
            self._local_offer = None
            self._cancel_timer("answer")
            settled = self._offer_settled
            self._offer_settled = None
            if settled is not None:
                settled.set()
            await self._flush_pending_candidates()
        if renegotiation:
            self._emit(SessionEventKind.RENEGOTIATED)

    async def _on_candidate(self, candidate: IceCandidate | None) -> None:
        if candidate is not None and not candidate.candidate:
            candidate = None
        async with self._negotiation:
            self._ensure_active()
            if not self._remote_applied:
                if len(self._pending_candidates) >= self._limits.max_pending_candidates:
                    raise WebRtcError(ErrorCode.QUEUE_EXHAUSTED)
                self._pending_candidates.append(candidate)
                return
            await self._add_candidate(candidate)

    async def _flush_pending_candidates(self) -> None:
        pending, self._pending_candidates = self._pending_candidates, []
        for candidate in pending:
            await self._add_candidate(candidate)

    async def _add_candidate(self, candidate: IceCandidate | None) -> None:
        if (
            candidate is not None
            and candidate.username_fragment is not None
            and self._remote_ufrags
            and candidate.username_fragment not in self._remote_ufrags
        ):
            logger.debug("Dropping a stale candidate for %s", self._label)
            return
        peer = self._require_peer()
        try:
            await peer.add_ice_candidate(candidate)
        except Exception:
            if self._ignored_offer:
                return
            logger.debug("The engine rejected a candidate for %s", self._label)
            self._report(ErrorCode.NEGOTIATION_FAILED)

    def _remote_description_applied(self, summary: DescriptionSummary) -> None:
        self._remote_applied = True
        self._remote_ufrags = summary.ufrags

    async def _renegotiate_if_needed(self) -> None:
        peer = self._peer
        if (
            peer is None
            or self.code is not None
            or peer.signaling_state != SignalingState.STABLE
            or not self._remote_applied
        ):
            return
        if peer.needs_negotiation():
            await self._renegotiate()

    async def _renegotiate(self, *, wait: bool = False) -> None:
        """Create a server offer if the engine has unnegotiated changes."""
        settled: asyncio.Event | None = None
        async with self._negotiation:
            self._ensure_active()
            peer = self._require_peer()
            initial = not self._remote_applied and self._local_offer is None
            if self._signaling_lost or not self._signaling_open:
                raise WebRtcError(ErrorCode.SIGNALING_LOST)
            if peer.signaling_state == SignalingState.HAVE_LOCAL_OFFER:
                settled = self._offer_settled
            elif peer.signaling_state != SignalingState.STABLE:
                # A remote offer is being answered; renegotiate afterwards.
                pass
            elif initial or peer.needs_negotiation():
                if not initial and not self._engine.capabilities.renegotiation:
                    raise WebRtcError(
                        ErrorCode.NEGOTIATION_FAILED,
                        "The engine does not support renegotiation.",
                    )
                self._negotiations += 1
                negotiation = self._negotiations
                offer = await self._describe(peer.create_offer)
                self._local_offer = negotiation
                settled = self._offer_settled = asyncio.Event()
                self._send(
                    protocol.DescriptionMessage(
                        description=protocol.DescriptionPayload.from_engine(offer),
                        negotiation=negotiation,
                    )
                )
                self._release_candidates()
                if not initial:
                    self._timer(
                        "answer",
                        self._limits.negotiation_timeout_seconds,
                        ErrorCode.NEGOTIATION_TIMEOUT,
                    )
        if wait and settled is not None:
            await self._until(settled)

    async def _describe(
        self, create: Callable[[], Awaitable[SessionDescription]]
    ) -> SessionDescription:
        """Create a local description while holding back local candidates, so
        that the remote peer always receives a description before its
        candidates."""
        self._held_candidates = []
        try:
            return await self._negotiate_call(create())
        except BaseException:
            self._release_candidates()
            raise

    def _release_candidates(self) -> None:
        held, self._held_candidates = self._held_candidates, None
        for candidate in held or ():
            self._send_candidate(candidate)

    def _send_candidate(self, candidate: IceCandidate | None) -> None:
        if self._held_candidates is not None:
            self._held_candidates.append(candidate)
            return
        self._send(
            protocol.CandidateMessage(
                candidate=None
                if candidate is None
                else protocol.CandidatePayload.from_engine(candidate)
            )
        )

    def _check_media(self, summary: DescriptionSummary) -> None:
        media = self.declaration.media
        for section in summary.sections:
            if section.kind == "application" or section.rejected:
                continue
            direction = media.direction(section.kind)
            if direction is None:
                if section.direction == TransceiverDirection.INACTIVE:
                    continue
                raise WebRtcError(
                    ErrorCode.MEDIA_REJECTED,
                    f"The session does not accept {section.kind} media.",
                )
            if section.author_sends and not direction.receives:
                raise WebRtcError(
                    ErrorCode.MEDIA_REJECTED,
                    f"The session does not receive {section.kind} media.",
                )
        for kind in ("audio", "video"):
            if summary.sending(kind) > media.max_incoming_tracks:
                raise WebRtcError(
                    ErrorCode.MEDIA_REJECTED,
                    f"Too many incoming {kind} tracks.",
                )

    # ------------------------------------------------------- engine callbacks

    def _on_local_candidate(self, candidate: IceCandidate | None) -> None:
        if self.code is None:
            self._send_candidate(candidate)

    def _on_connection_state(self, state: ConnectionState) -> None:
        if self.code is not None:
            return
        self._connection_state = state
        if state == ConnectionState.CONNECTED:
            self._ever_connected = True
            self._cancel_timer("transport")
            self._check_established()
        elif state == ConnectionState.DISCONNECTED:
            self._timer(
                "transport",
                self._limits.transport_loss_grace_seconds,
                ErrorCode.TRANSPORT_LOST,
            )
        elif state == ConnectionState.FAILED:
            if not self._ever_connected:
                self.terminate(ErrorCode.NEGOTIATION_FAILED)
            elif self._engine.capabilities.ice_restart:
                self._timer(
                    "transport",
                    self._limits.transport_loss_grace_seconds,
                    ErrorCode.TRANSPORT_LOST,
                )
            else:
                self.terminate(ErrorCode.TRANSPORT_LOST)
        elif state == ConnectionState.CLOSED:
            self.terminate(ErrorCode.TRANSPORT_LOST)

    def _on_track(self, track: EngineTrack) -> None:
        if self.code is not None:
            track.stop()
            return
        direction = self.declaration.media.direction(track.kind)
        if direction is None or not direction.receives:
            track.stop()
            self.terminate(ErrorCode.MEDIA_REJECTED)
            return
        if (
            self._incoming_counts[track.kind]
            >= self.declaration.media.max_incoming_tracks
        ):
            track.stop()
            self.terminate(ErrorCode.MEDIA_REJECTED)
            return
        self._incoming_counts[track.kind] += 1
        counted = True

        def ended() -> None:
            nonlocal counted
            if counted:
                counted = False
                self._incoming_counts[track.kind] -= 1

        track.on_ended(ended)
        incoming = IncomingTrack(track)
        self.incoming.append(incoming)
        self._emit(SessionEventKind.TRACK, media_kind=track.kind)
        if self._started.is_set():
            self._dispatch_track(incoming)
        else:
            self._queued_tracks.append(incoming)

    def _on_remote_channel(self, engine_channel: EngineDataChannel) -> None:
        label = engine_channel.label
        channel = self._channels.get(label)
        if (
            self.code is not None
            or channel is None
            or channel.engine is not None
            or self.declaration.server_creates(channel.spec)
        ):
            with contextlib.suppress(Exception):
                engine_channel.close()
            if self.code is None:
                self._report(ErrorCode.CHANNEL_REJECTED, channel=label[:63])
            return
        self._bind_channel(channel, engine_channel)

    def _bind_channel(
        self, channel: _Channel, engine_channel: EngineDataChannel
    ) -> None:
        channel.engine = engine_channel
        engine_channel.set_buffered_amount_low_threshold(
            self._limits.send_buffer_high_bytes
        )

        def opened() -> None:
            channel.opened.set()
            self._check_established()

        def closed() -> None:
            channel.closed.set()
            channel.drained.set()

        engine_channel.observe(
            on_open=opened,
            on_message=lambda data: self._on_channel_message(channel, data),
            on_close=closed,
            on_buffered_amount_low=channel.drained.set,
        )
        if engine_channel.ready_state == ChannelState.OPEN:
            opened()

    def _on_channel_message(self, channel: _Channel, data: str | bytes) -> None:
        if self.code is not None:
            return
        name = channel.spec.name
        size = len(data.encode()) if isinstance(data, str) else len(data)
        if size > channel.max_bytes:
            self._report(ErrorCode.MESSAGE_TOO_LARGE, channel=name)
            return
        try:
            message = _decode(channel.spec, data)
        except WebRtcError as error:
            self._report(error.code, channel=name)
            return
        try:
            channel.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.terminate(ErrorCode.QUEUE_EXHAUSTED)

    # --------------------------------------------------------------- handlers

    def _check_established(self) -> None:
        if self._established or self.code is not None:
            return
        if self._connection_state != ConnectionState.CONNECTED:
            return
        if not all(channel.opened.is_set() for channel in self._channels.values()):
            return
        self._established = True
        self._cancel_timer("negotiation")
        self._emit(SessionEventKind.CONNECTED)
        self._spawn(self._begin(), "start", ErrorCode.HANDLER_FAILURE)

    async def _begin(self) -> None:
        session = self._session
        if session.on_start is not None:
            await session.on_start(self._peer_handle)
        self._started.set()
        self._send(protocol.ReadyMessage())
        self._emit(SessionEventKind.READY)
        for name, handler in session.message_handlers.items():
            self._spawn_handler(
                self._consume(self._channels[name], handler), f"message:{name}"
            )
        queued, self._queued_tracks = self._queued_tracks, []
        for track in queued:
            self._dispatch_track(track)
        if self._limits.media_timeout_seconds is not None:
            self._spawn(self._watch_media(), "media", ErrorCode.ENGINE_FAILURE)
        if session.run is not None:
            self._spawn_handler(self._run_handler(session.run), "run")

    async def _run_handler(self, run: Callable[[Peer], Awaitable[None]]) -> None:
        await run(self._peer_handle)
        self.terminate(ErrorCode.CLOSED)

    async def _consume(
        self, channel: _Channel, handler: Callable[[Peer, Any], Awaitable[None]]
    ) -> None:
        while True:
            message = await channel.queue.get()
            await handler(self._peer_handle, message)

    def _dispatch_track(self, track: IncomingTrack) -> None:
        handler = self._session.on_track
        if handler is None:
            self._spawn(_drain(track), "drain", ErrorCode.ENGINE_FAILURE)
        else:
            self._spawn_handler(handler(self._peer_handle, track), "track")

    async def _watch_media(self) -> None:
        expected: list[MediaKind] = []
        media = self.declaration.media
        if media.audio is not None and media.audio.receives:
            expected.append(MediaKind.AUDIO)
        if media.video is not None and media.video.receives:
            expected.append(MediaKind.VIDEO)
        if not expected:
            return
        deadline = self._limits.media_timeout_seconds
        assert deadline is not None
        try:
            async with asyncio.timeout(deadline):
                while True:
                    stats = await self.stats()
                    if all(stats.received(kind) > 0 for kind in expected):
                        break
                    await asyncio.sleep(_MEDIA_POLL_SECONDS)
        except TimeoutError:
            self.terminate(ErrorCode.MEDIA_TIMEOUT)
            return
        self._emit(SessionEventKind.MEDIA_READY)

    # ------------------------------------------------------------ task helpers

    def _spawn(self, coroutine: Awaitable[None], name: str, failure: ErrorCode) -> None:
        task = asyncio.create_task(
            self._guard(coroutine, name, failure), name=f"webrtc:{self._label}:{name}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_handler(self, coroutine: Awaitable[None], name: str) -> None:
        task = asyncio.create_task(
            self._guard(coroutine, name, ErrorCode.HANDLER_FAILURE),
            name=f"webrtc:{self._label}:{name}",
        )
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _guard(
        self, coroutine: Awaitable[None], name: str, failure: ErrorCode
    ) -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except SessionClosedError:
            pass
        except WebRtcError as error:
            if failure is ErrorCode.HANDLER_FAILURE:
                logger.warning(
                    "Handler %s of %s raised %s", name, self._label, error.code.value
                )
                self.terminate(ErrorCode.HANDLER_FAILURE)
            else:
                self.terminate(error.code)
        except Exception:
            if failure is ErrorCode.HANDLER_FAILURE:
                logger.exception("Handler %s of %s failed", name, self._label)
            else:
                logger.exception("Session task %s of %s failed", name, self._label)
            self.terminate(failure)

    def _timer(self, name: str, seconds: float, code: ErrorCode) -> None:
        self._cancel_timer(name)

        async def expire() -> None:
            await asyncio.sleep(seconds)
            self._timers.pop(name, None)
            logger.info("Session %s hit its %s deadline", self._label, name)
            self.terminate(code)

        task = asyncio.create_task(expire(), name=f"webrtc:{self._label}:{name}")
        self._timers[name] = task

    def _cancel_timer(self, name: str) -> None:
        task = self._timers.pop(name, None)
        if task is not None:
            task.cancel()

    async def _until(self, *events: asyncio.Event) -> None:
        """Wait for any of ``events``; raise once the session terminates."""
        if not any(event.is_set() for event in events):
            await self._first(self._done, *events)
        self._ensure_active()

    @staticmethod
    async def _first(*events: asyncio.Event) -> None:
        waiters = [asyncio.ensure_future(event.wait()) for event in events]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    def _ensure_active(self) -> None:
        if self.code is not None:
            raise SessionClosedError(self.code)

    def _require_peer(self) -> EnginePeer:
        if self._peer is None:
            raise SessionClosedError(self.code or ErrorCode.CLOSED)
        return self._peer

    def _engine_call[T](self, call: Callable[[], T]) -> T:
        try:
            return call()
        except WebRtcError:
            raise
        except Exception as error:
            logger.exception("Engine operation failed for %s", self._label)
            raise WebRtcError(ErrorCode.ENGINE_FAILURE) from error

    async def _negotiate_call[T](self, call: Awaitable[T]) -> T:
        try:
            return await call
        except WebRtcError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.info(
                "Negotiation failed for %s: %s", self._label, type(error).__name__
            )
            raise WebRtcError(ErrorCode.NEGOTIATION_FAILED) from error

    def _emit(
        self,
        kind: SessionEventKind,
        *,
        code: ErrorCode | None = None,
        media_kind: MediaKind | None = None,
    ) -> None:
        hook = self._options.on_event
        if hook is None:
            return
        event = SessionEvent(
            kind=kind,
            name=self.declaration.name,
            session_id=self.session_id,
            elapsed_seconds=time.monotonic() - self._admitted_at,
            code=code,
            media_kind=media_kind,
        )
        try:
            hook(event)
        except Exception:
            logger.exception("Session event hook failed")

    @property
    def _label(self) -> str:
        return f"{self.declaration.name}/{self.session_id[:8]}"

    # ---------------------------------------------------------------- cleanup

    async def _cleanup_once(self) -> None:
        if self._finished.is_set():
            return
        try:
            await self._cleanup()
        finally:
            self._finished.set()

    async def _cleanup(self) -> None:
        code = self.code or ErrorCode.CLOSED
        self.code = code
        self._done.set()
        for name in list(self._timers):
            self._cancel_timer(name)
        settled = self._offer_settled
        if settled is not None:
            settled.set()
        await self._close_signaling(code)
        deadline = self._limits.shutdown_timeout_seconds
        started = time.monotonic()

        def remaining() -> float:
            return max(0.0, deadline - (time.monotonic() - started))

        pending = {*self._handlers, *self._tasks}
        for task in pending:
            task.cancel()
        if pending:
            _, stuck = await asyncio.wait(pending, timeout=remaining())
            if stuck:
                logger.warning(
                    "%d task(s) of %s ignored cancellation", len(stuck), self._label
                )
        if self._session.on_close is not None and self._accepted:
            try:
                async with asyncio.timeout(
                    max(remaining(), MINIMUM_CLEANUP_STEP_SECONDS)
                ):
                    await self._session.on_close(self._peer_handle, code)
            except TimeoutError:
                logger.warning("on_close of %s exceeded its deadline", self._label)
            except Exception:
                logger.exception("on_close of %s failed", self._label)
        for track in self.incoming:
            with contextlib.suppress(Exception):
                track._stop()
        for handle in self._outgoing:
            handle.removed = True
        for native in list(self._owned_tracks.values()):
            self._release_track(native)
        for channel in self._channels.values():
            channel.closed.set()
            channel.drained.set()
            if channel.engine is not None:
                with contextlib.suppress(Exception):
                    channel.engine.close()
        if self._peer is not None:
            try:
                async with asyncio.timeout(
                    max(remaining(), MINIMUM_CLEANUP_STEP_SECONDS)
                ):
                    await self._peer.close()
            except TimeoutError:
                logger.warning(
                    "Engine cleanup of %s exceeded its deadline", self._label
                )
            except Exception:
                logger.exception("Engine cleanup of %s failed", self._label)
        self._slots.release()
        self._emit(SessionEventKind.CLOSED, code=code)
        log = (
            logger.info
            if code in (ErrorCode.CLOSED, ErrorCode.SHUTDOWN)
            else (logger.warning)
        )
        log("Session %s closed: %s", self._label, code.value)

    async def _close_signaling(self, code: ErrorCode) -> None:
        self._signaling_open = False
        if self._ws.application_state != WebSocketState.CONNECTED:
            return
        if self._ws.client_state != WebSocketState.CONNECTED:
            return
        with contextlib.suppress(Exception):
            await self._ws.close(code=close_code(code), reason=code.value)


class _Observer:
    """Forwards engine callbacks into the runtime."""

    def __init__(self, runtime: SessionRuntime) -> None:
        self._runtime = runtime

    def ice_candidate(self, candidate: IceCandidate | None) -> None:
        self._runtime._on_local_candidate(candidate)

    def connection_state(self, state: ConnectionState) -> None:
        self._runtime._on_connection_state(state)

    def track(self, track: EngineTrack) -> None:
        self._runtime._on_track(track)

    def data_channel(self, channel: EngineDataChannel) -> None:
        self._runtime._on_remote_channel(channel)


async def _drain(track: IncomingTrack) -> None:
    with contextlib.suppress(Exception):
        while not track.ended:
            await track.recv()


def _stop_native(native: Any) -> None:
    stop = getattr(native, "stop", None)
    if callable(stop):
        with contextlib.suppress(Exception):
            stop()


def _encode(spec: DataChannelSpec, message: Any) -> str | bytes:
    fmt = spec.send_format
    if fmt == MessageFormat.NONE:
        raise WebRtcError(
            ErrorCode.INVALID_MESSAGE, f"Channel {spec.name!r} does not send messages."
        )
    if fmt == MessageFormat.RAW:
        if isinstance(message, str | bytes):
            return message
        if isinstance(message, bytearray | memoryview):
            return bytes(message)
        raise WebRtcError(
            ErrorCode.INVALID_MESSAGE, "Raw channels send str or bytes values."
        )
    if fmt == MessageFormat.TEXT:
        if isinstance(message, str):
            return message
        raise WebRtcError(ErrorCode.INVALID_MESSAGE, "Text channels send str values.")
    if fmt == MessageFormat.BINARY:
        if isinstance(message, bytes | bytearray | memoryview):
            return bytes(message)
        raise WebRtcError(
            ErrorCode.INVALID_MESSAGE, "Binary channels send bytes values."
        )
    adapter = spec.send_adapter
    assert adapter is not None
    try:
        value = adapter.validate_python(message)
        return adapter.dump_json(value, by_alias=True).decode()
    except ValidationError as error:
        raise WebRtcError(ErrorCode.INVALID_MESSAGE) from error


def _decode(spec: DataChannelSpec, data: str | bytes) -> Any:
    fmt = spec.receive_format
    if fmt == MessageFormat.NONE:
        raise WebRtcError(ErrorCode.INVALID_MESSAGE)
    if fmt == MessageFormat.RAW:
        return data
    if fmt == MessageFormat.TEXT:
        if isinstance(data, str):
            return data
        raise WebRtcError(ErrorCode.INVALID_MESSAGE)
    if fmt == MessageFormat.BINARY:
        if isinstance(data, bytes):
            return data
        raise WebRtcError(ErrorCode.INVALID_MESSAGE)
    if not isinstance(data, str):
        raise WebRtcError(ErrorCode.INVALID_MESSAGE)
    adapter = spec.receive_adapter
    assert adapter is not None
    try:
        return adapter.validate_json(data)
    except ValidationError:
        raise WebRtcError(ErrorCode.INVALID_MESSAGE) from None
