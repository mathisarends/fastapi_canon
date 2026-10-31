"""Inject your remote subscription service into this application factory."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from fastapi import FastAPI
from fastapi_canon.integrations.webrtc import (
    Direction,
    MediaPolicy,
    Role,
    Session,
    WebRtcRouter,
)

from .engine import RemoteVideoEngine, VideoLease


class VideoService(Protocol):
    def subscribe(self, browser_id: str) -> AbstractAsyncContextManager[VideoLease]: ...


def create_router(service: VideoService) -> WebRtcRouter:
    rtc = WebRtcRouter(max_sessions=1)

    @rtc.session(
        "/browser/{browser_id}/video",
        role=Role.OFFERER,
        media=MediaPolicy(video=Direction.SEND),
    )
    async def video(browser_id: str) -> AsyncIterator[Session]:
        # Production applications authorize first, using a short unit of work.
        # subscribe() is per viewer; exiting closes only this subscription.
        async with service.subscribe(browser_id) as lease:
            yield Session(engine=RemoteVideoEngine(lease))

    return rtc


def create_app(service: VideoService) -> FastAPI:
    app = FastAPI()
    app.include_router(create_router(service))
    return app
