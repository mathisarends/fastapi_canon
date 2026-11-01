"""Typed WebRTC sessions for FastAPI.

Declare sessions on a :class:`WebRtcRouter`; the router provides WebSocket
signaling, admission through FastAPI dependencies, limits and cleanup, while an
engine adapter (see :mod:`fastapi_canon.integrations.webrtc.engines`) owns the
peer connection.
"""

from fastapi_canon._version import __version__
from fastapi_canon.integrations.webrtc.canon import CanonWebRtcRouter
from fastapi_canon.integrations.webrtc.declarations import (
    AdmissionErrors,
    DataChannelSpec,
    Direction,
    MediaPolicy,
    MessageFormat,
    Role,
    Session,
    SessionContext,
    SessionDeclaration,
    SessionLimits,
)
from fastapi_canon.integrations.webrtc.engine import (
    CandidatePairStats,
    ChannelState,
    ConnectionState,
    ConnectionStats,
    DescriptionType,
    Engine,
    EngineCapabilities,
    MediaKind,
    MediaStats,
    SignalingState,
    TransceiverDirection,
)
from fastapi_canon.integrations.webrtc.errors import (
    ErrorCode,
    SessionClosedError,
    WebRtcError,
)
from fastapi_canon.integrations.webrtc.events import (
    EventHook,
    SessionEvent,
    SessionEventKind,
)
from fastapi_canon.integrations.webrtc.ice import (
    IceServer,
    IceServers,
    IceServerSource,
    TurnRestCredentials,
)
from fastapi_canon.integrations.webrtc.peer import (
    Channel,
    IncomingTrack,
    OutgoingTrack,
    Peer,
)
from fastapi_canon.integrations.webrtc.protocol import PROTOCOL_VERSION, SUBPROTOCOL
from fastapi_canon.integrations.webrtc.router import WebRtcRouter

__all__ = [
    "PROTOCOL_VERSION",
    "SUBPROTOCOL",
    "AdmissionErrors",
    "CandidatePairStats",
    "CanonWebRtcRouter",
    "Channel",
    "ChannelState",
    "ConnectionState",
    "ConnectionStats",
    "DataChannelSpec",
    "DescriptionType",
    "Direction",
    "Engine",
    "EngineCapabilities",
    "ErrorCode",
    "EventHook",
    "IceServer",
    "IceServerSource",
    "IceServers",
    "IncomingTrack",
    "MediaKind",
    "MediaPolicy",
    "MediaStats",
    "MessageFormat",
    "OutgoingTrack",
    "Peer",
    "Role",
    "Session",
    "SessionClosedError",
    "SessionContext",
    "SessionDeclaration",
    "SessionEvent",
    "SessionEventKind",
    "SessionLimits",
    "SignalingState",
    "TransceiverDirection",
    "TurnRestCredentials",
    "WebRtcError",
    "WebRtcRouter",
    "__version__",
]
