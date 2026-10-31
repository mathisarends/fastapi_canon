"""An application with every contract feature, importable by CLI tests."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query
from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Direction,
    MediaPolicy,
    Role,
    Session,
    WebRtcRouter,
)
from pydantic import BaseModel, Field

from tests.fake_engine import FakeEngine


class Point(BaseModel):
    """A position on the board."""

    x: int
    y: int


class Move(BaseModel):
    kind: Literal["move"] = "move"
    to: Point
    note: str | None = None


class Chat(BaseModel):
    kind: Literal["chat"] = "chat"
    text: str = Field(description="What the player said.")


class State(BaseModel):
    turn: int = 0
    """Always present in server messages, despite its default."""
    pieces: dict[str, Point]
    last: tuple[int, int] | None = None


def player(
    game_id: Annotated[str, Path()],
    seat: Annotated[int, Query(ge=0, le=3, description="Seat at the table.")] = 0,
    user_agent: Annotated[str | None, Header()] = None,
) -> str:
    return f"{game_id}:{seat}"


def token(token: Annotated[str, Query()]) -> str:
    return token


rtc = WebRtcRouter(engine=FakeEngine(), prefix="/rtc")


@rtc.session(
    "/games/{game_id}",
    media=MediaPolicy(audio=Direction.SEND_RECEIVE, max_incoming_tracks=1),
    data_channels=(
        DataChannelSpec(
            name="moves",
            receive_model=Annotated[Move | Chat, Field(discriminator="kind")],
            send_model=State,
            ordered=False,
            max_retransmits=0,
        ),
        DataChannelSpec(name="log", send_model=str, negotiated=False),
        DataChannelSpec(name="blob", receive_model=bytes, max_message_bytes=1024),
    ),
)
def game(
    seat: Annotated[str, Depends(player)],
    _token: Annotated[str, Depends(token)],
) -> Session:
    """Play a game.

    Moves and chat share one unordered channel."""
    return Session()


@rtc.session("/screen", name="screen_share", role=Role.OFFERER)
def screen() -> Session:
    return Session()


api = APIRouter(prefix="/api")
api.include_router(rtc)
app = FastAPI()
app.include_router(api)
