"""A language-practice tutor with two WebRTC sessions.

``practice`` (the browser offers): the learner's microphone is echoed back and
typed prompts on the ``messages`` channel receive feedback. Admission runs
through ordinary FastAPI dependencies before any peer resources exist.

``clock`` (the server offers): a synthetic video track plus a ``ticks``
channel, for dashboards that only watch.

Run it with ``uvicorn examples.practice.app:app`` from the package directory and
regenerate the browser client with::

    fastapi-webrtc contract --app examples.practice.app:app \
        --output examples/practice/contract.json
    fastapi-webrtc generate --contract examples/practice/contract.json \
        --output examples/practice/client --client-name PracticeRtcClient
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Never

from aiortc import VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Direction,
    IncomingTrack,
    MediaPolicy,
    Peer,
    Role,
    Session,
    SessionLimits,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine
from pydantic import BaseModel, Field

DEMO_TOKEN = "demo"


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str


LESSONS = {
    "greetings": Lesson("greetings", "Greetings"),
    "numbers": Lesson("numbers", "Numbers"),
}


class Prompt(BaseModel):
    """A sentence the learner typed."""

    text: str = Field(min_length=1, max_length=500)


class Feedback(BaseModel):
    """The tutor's answer to a prompt."""

    lesson: str
    text: str
    words: int = Field(description="Number of words in the prompt.")
    audio_frames: int = Field(
        default=0, description="Audio frames received from the learner so far."
    )


class Tick(BaseModel):
    sequence: int
    lesson_count: int


# Specs type the channel handles, e.g. `peer.send_message(MESSAGES, Feedback(...))`.
MESSAGES = DataChannelSpec("messages", receive_model=Prompt, send_model=Feedback)
# `Never` makes the clock one-way: the browser cannot send ticks back.
TICKS: DataChannelSpec[Never, Tick] = DataChannelSpec(
    "ticks", receive_model=Never, send_model=Tick
)


def lesson_access(
    lesson_id: str, token: Annotated[str | None, Query()] = None
) -> Lesson:
    """Admission: runs before the WebSocket is accepted."""
    lesson = LESSONS.get(lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Unknown lesson")
    if token != DEMO_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    return lesson


rtc = WebRtcRouter(
    prefix="/rtc",
    engine=AiortcEngine(),
    limits=SessionLimits(negotiation_timeout_seconds=20),
    max_sessions=50,
)


@rtc.session(
    "/lessons/{lesson_id}",
    name="practice",
    media=MediaPolicy(audio=Direction.SEND_RECEIVE),
    data_channels=(MESSAGES,),
)
def practice(lesson: Annotated[Lesson, Depends(lesson_access)]) -> Session:
    """Speak with the tutor: audio is echoed back and prompts receive feedback."""
    relay = MediaRelay()
    frames = 0

    async def on_track(peer: Peer, track: IncomingTrack) -> None:
        nonlocal frames
        # Echo through a relay, so this handler can also count the frames.
        await peer.send_track(relay.subscribe(track.native, buffered=False))
        counter = relay.subscribe(track.native, buffered=False)
        try:
            while True:
                await counter.recv()
                frames += 1
        except MediaStreamError:
            return  # The learner's track ended.

    async def on_prompt(peer: Peer, prompt: Prompt) -> None:
        await peer.send_message(
            MESSAGES,
            Feedback(
                lesson=lesson.title,
                text=f"You said: {prompt.text}",
                words=len(prompt.text.split()),
                audio_frames=frames,
            ),
        )

    return Session(on_track=on_track, on_message={MESSAGES: on_prompt})


@rtc.session(
    "/clock",
    name="clock",
    role=Role.OFFERER,
    media=MediaPolicy(video=Direction.SEND),
    data_channels=(TICKS,),
)
def clock(interval_ms: Annotated[int, Query(ge=50, le=5000)] = 250) -> Session:
    """Watch a synthetic video track and receive a tick on every interval."""

    async def run(peer: Peer) -> None:
        sequence = 0
        while not peer.closed:
            await peer.send_message(
                TICKS, Tick(sequence=sequence, lesson_count=len(LESSONS))
            )
            sequence += 1
            await asyncio.sleep(interval_ms / 1000)

    return Session(run=run, tracks=[VideoStreamTrack()])


app = FastAPI(title="Practice tutor")
app.include_router(rtc)
