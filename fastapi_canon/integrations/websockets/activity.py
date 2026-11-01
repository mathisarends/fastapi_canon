import logging
from collections.abc import Sequence

from fastapi_canon.integrations.websockets.constants import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


class _Activity:
    def __init__(self, middleware: Sequence[object]) -> None:
        self._middleware = tuple(middleware)

    async def emit(self, method: str, *args: object) -> None:
        for item in self._middleware:
            callback = getattr(item, method, None)
            if callback is None:
                continue
            try:
                await callback(*args)
            except Exception:
                logger.exception("RPC middleware %s callback failed", method)


def _activity_for(middleware: Sequence[object]) -> _Activity | None:
    return _Activity(middleware) if middleware else None


async def notify_activity(
    activity: _Activity | None, method: str, *args: object
) -> None:
    if activity is not None:
        await activity.emit(method, *args)
