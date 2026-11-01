from types import MappingProxyType

from fastapi_canon.integrations.websockets.connection import (
    REJECTION_CLOSES,
    RpcConnectionClose,
)

CLOSE_CODES = MappingProxyType(
    {
        RpcConnectionClose.NORMAL: 1000,
        RpcConnectionClose.SHUTDOWN: 1001,
        RpcConnectionClose.SERVICE_RESTART: 1012,
        RpcConnectionClose.PROTOCOL_ERROR: 1002,
        RpcConnectionClose.POLICY_VIOLATION: 1008,
        RpcConnectionClose.MESSAGE_TOO_BIG: 1009,
        RpcConnectionClose.INTERNAL_ERROR: 1011,
        RpcConnectionClose.TRY_AGAIN_LATER: 1013,
        RpcConnectionClose.OTHER: 1008,
    }
)
REJECTION_CLOSE_CODES = MappingProxyType(
    {rejection: CLOSE_CODES[close] for rejection, close in REJECTION_CLOSES.items()}
)
MAX_CLOSE_REASON_BYTES = 123


def close_reason(reason: str) -> str:
    return reason.encode("utf-8")[:MAX_CLOSE_REASON_BYTES].decode(
        "utf-8", errors="ignore"
    )
