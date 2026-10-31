"""Signaling protocol ``fastapi-webrtc.v1``: JSON text frames over one WebSocket.

See ``docs/protocol.md`` for the normative description. Every message is a JSON
object with a ``type`` field. Unknown fields are ignored so that later minor
protocol revisions can add optional fields; unknown ``type`` values are a
``protocol_error``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from fastapi_canon.integrations.webrtc.declarations import (
    Direction,
    MessageFormat,
    Role,
)
from fastapi_canon.integrations.webrtc.engine import (
    DescriptionType,
    IceCandidate,
    SessionDescription,
)
from fastapi_canon.integrations.webrtc.errors import ErrorCode
from fastapi_canon.integrations.webrtc.ice import IceServer

PROTOCOL_VERSION = 1
SUBPROTOCOL = "fastapi-webrtc.v1"


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class DescriptionPayload(_Message):
    type: DescriptionType
    sdp: str = Field(max_length=1024 * 1024)

    def to_engine(self) -> SessionDescription:
        return SessionDescription(type=self.type, sdp=self.sdp)

    @classmethod
    def from_engine(cls, description: SessionDescription) -> DescriptionPayload:
        return cls(type=description.type, sdp=description.sdp)


class CandidatePayload(_Message):
    """The browser's ``RTCIceCandidateInit``."""

    candidate: str = Field(max_length=4096)
    sdp_mid: str | None = Field(default=None, alias="sdpMid", max_length=256)
    sdp_mline_index: int | None = Field(
        default=None, alias="sdpMLineIndex", ge=0, le=1024
    )
    username_fragment: str | None = Field(
        default=None, alias="usernameFragment", max_length=256
    )

    def to_engine(self) -> IceCandidate:
        return IceCandidate(
            candidate=self.candidate,
            sdp_mid=self.sdp_mid,
            sdp_mline_index=self.sdp_mline_index,
            username_fragment=self.username_fragment,
        )

    @classmethod
    def from_engine(cls, candidate: IceCandidate) -> CandidatePayload:
        return cls(
            candidate=candidate.candidate,
            sdpMid=candidate.sdp_mid,
            sdpMLineIndex=candidate.sdp_mline_index,
            usernameFragment=candidate.username_fragment,
        )


class DescriptionMessage(_Message):
    """An offer or answer. Answers echo the ``negotiation`` of their offer."""

    type: Literal["description"] = "description"
    description: DescriptionPayload
    negotiation: int = Field(ge=0)


class CandidateMessage(_Message):
    """A trickled candidate, or ``null`` for end-of-candidates."""

    type: Literal["candidate"] = "candidate"
    candidate: CandidatePayload | None


type ClientMessage = Annotated[
    DescriptionMessage | CandidateMessage, Field(discriminator="type")
]
client_message_adapter: TypeAdapter[DescriptionMessage | CandidateMessage] = (
    TypeAdapter(ClientMessage)
)


class MediaDescriptor(_Message):
    audio: Direction | None
    video: Direction | None
    max_incoming_tracks: int


class ChannelDescriptor(_Message):
    name: str
    id: int | None
    negotiated: bool
    ordered: bool
    max_retransmits: int | None
    max_packet_life_time_ms: int | None
    max_message_bytes: int
    receive_format: MessageFormat
    """Format the server accepts (browser to server); ``none`` accepts nothing."""
    send_format: MessageFormat
    """Format the server sends (server to browser)."""


class LimitsDescriptor(_Message):
    negotiation_timeout_ms: int
    max_signaling_message_bytes: int
    max_pending_candidates: int


class CapabilitiesDescriptor(_Message):
    trickle_ice: bool
    ice_restart: bool
    renegotiation: bool


class SessionMessage(_Message):
    """The first server message, sent once the session was admitted."""

    type: Literal["session"] = "session"
    protocol: int = PROTOCOL_VERSION
    session_id: str
    name: str
    role: Role
    ice_servers: tuple[IceServer, ...]
    media: MediaDescriptor
    channels: tuple[ChannelDescriptor, ...]
    limits: LimitsDescriptor
    capabilities: CapabilitiesDescriptor


class ReadyMessage(_Message):
    """The transport is connected, channels are open and ``on_start`` finished."""

    type: Literal["ready"] = "ready"


class ErrorMessage(_Message):
    """A non-terminal failure. Terminal failures close the WebSocket instead."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    channel: str | None = None


type ServerMessage = (
    SessionMessage | ReadyMessage | DescriptionMessage | CandidateMessage | ErrorMessage
)


def encode(message: ServerMessage) -> str:
    return message.model_dump_json(by_alias=True)
