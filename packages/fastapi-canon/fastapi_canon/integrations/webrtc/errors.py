"""Stable error categories shared by the server runtime and generated clients."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Why a session ended, or why a single operation failed.

    The value is the WebSocket close reason and the ``code`` of signaling
    ``error`` messages. Values are part of the wire contract.
    """

    CLOSED = "closed"
    """Normal termination by either side or by the session's ``run`` handler."""
    SHUTDOWN = "shutdown"
    """The application is shutting down."""
    PROTOCOL_ERROR = "protocol_error"
    """A signaling message was malformed or arrived in an invalid state."""
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    """The client requested only signaling protocol versions the server lacks."""
    SESSION_LIMIT = "session_limit"
    """The endpoint or router reached its concurrent-session limit."""
    NEGOTIATION_FAILED = "negotiation_failed"
    """The engine rejected a description or candidate."""
    NEGOTIATION_TIMEOUT = "negotiation_timeout"
    """The connection and declared channels were not ready before the deadline."""
    MEDIA_TIMEOUT = "media_timeout"
    """Expected incoming media did not arrive before the media deadline."""
    MEDIA_REJECTED = "media_rejected"
    """The remote description or a track violated the session's media policy."""
    TRANSPORT_LOST = "transport_lost"
    """The ICE/DTLS transport failed or stayed disconnected past its grace period."""
    SIGNALING_LOST = "signaling_lost"
    """The signaling connection dropped without a normal close."""
    ENGINE_FAILURE = "engine_failure"
    """The engine raised an unexpected error."""
    HANDLER_FAILURE = "handler_failure"
    """An application callback raised an exception."""
    QUEUE_EXHAUSTED = "queue_exhausted"
    """A bounded candidate, signaling or message queue overflowed."""
    SLOW_CONSUMER = "slow_consumer"
    """A data channel's send buffer did not drain before the send deadline."""
    MESSAGE_TOO_LARGE = "message_too_large"
    """A message exceeded its configured size limit."""
    INVALID_MESSAGE = "invalid_message"
    """A data-channel message failed decoding or validation."""
    CHANNEL_REJECTED = "channel_rejected"
    """The remote peer opened a data channel the session did not declare."""
    CHANNEL_CLOSED = "channel_closed"
    """A message was sent on a channel that is not open."""


_CLOSE_CODES = {
    ErrorCode.CLOSED: 1000,
    ErrorCode.SHUTDOWN: 1001,
    ErrorCode.UNSUPPORTED_PROTOCOL: 1002,
    ErrorCode.PROTOCOL_ERROR: 1008,
    ErrorCode.MEDIA_REJECTED: 1008,
    ErrorCode.CHANNEL_REJECTED: 1008,
    ErrorCode.MESSAGE_TOO_LARGE: 1009,
    ErrorCode.ENGINE_FAILURE: 1011,
    ErrorCode.HANDLER_FAILURE: 1011,
    ErrorCode.SESSION_LIMIT: 1013,
}

SESSION_FAILURE_CLOSE_CODE = 4000
"""Close code for WebRTC-specific session failures; the reason holds the category."""


def close_code(code: ErrorCode) -> int:
    """Return the WebSocket close code that accompanies a terminal error code."""
    return _CLOSE_CODES.get(code, SESSION_FAILURE_CLOSE_CODE)


_MESSAGES = {
    ErrorCode.CLOSED: "The session was closed.",
    ErrorCode.SHUTDOWN: "The server is shutting down.",
    ErrorCode.PROTOCOL_ERROR: "The signaling message was invalid.",
    ErrorCode.UNSUPPORTED_PROTOCOL: "The signaling protocol version is unsupported.",
    ErrorCode.SESSION_LIMIT: "Too many concurrent sessions.",
    ErrorCode.NEGOTIATION_FAILED: "WebRTC negotiation failed.",
    ErrorCode.NEGOTIATION_TIMEOUT: "WebRTC negotiation timed out.",
    ErrorCode.MEDIA_TIMEOUT: "Expected media did not arrive in time.",
    ErrorCode.MEDIA_REJECTED: "The media does not match the session policy.",
    ErrorCode.TRANSPORT_LOST: "The WebRTC transport was lost.",
    ErrorCode.SIGNALING_LOST: "The signaling connection was lost.",
    ErrorCode.ENGINE_FAILURE: "The WebRTC engine failed.",
    ErrorCode.HANDLER_FAILURE: "The session handler failed.",
    ErrorCode.QUEUE_EXHAUSTED: "A bounded session queue overflowed.",
    ErrorCode.SLOW_CONSUMER: "The remote peer did not consume messages in time.",
    ErrorCode.MESSAGE_TOO_LARGE: "The message exceeds the size limit.",
    ErrorCode.INVALID_MESSAGE: "The message is invalid.",
    ErrorCode.CHANNEL_REJECTED: "The data channel is not declared by the session.",
    ErrorCode.CHANNEL_CLOSED: "The data channel is not open.",
}


def default_message(code: ErrorCode) -> str:
    """A safe, generic description that never includes connection details."""
    return _MESSAGES[code]


class WebRtcError(Exception):
    """A categorized session or operation failure.

    ``message`` is shown to remote peers. Keep it free of SDP, ICE addresses and
    credentials; the default per-code messages satisfy that rule.
    """

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        self.code = ErrorCode(code)
        self.message = message or default_message(self.code)
        super().__init__(f"{self.code.value}: {self.message}")


class SessionClosedError(WebRtcError):
    """Raised by peer operations after the session has terminated."""

    def __init__(self, code: ErrorCode = ErrorCode.CLOSED) -> None:
        super().__init__(code)
