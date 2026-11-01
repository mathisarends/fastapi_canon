from typing import TYPE_CHECKING, Any

from fastapi_canon._version import __version__

if TYPE_CHECKING:
    from fastapi_canon.integrations.sse.canon import (
        CanonStreamRouter as CanonStreamRouter,
    )
    from fastapi_canon.integrations.sse.router import StreamRouter as StreamRouter

__all__ = ["CanonStreamRouter", "StreamRouter", "__version__"]


def __getattr__(name: str) -> Any:
    if name == "CanonStreamRouter":
        from fastapi_canon.integrations.sse.canon import CanonStreamRouter

        return CanonStreamRouter
    if name == "StreamRouter":
        from fastapi_canon.integrations.sse.router import StreamRouter

        return StreamRouter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
