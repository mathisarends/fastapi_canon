from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Literal

import pytest
from fastapi_canon.integrations.websockets import Inject, RpcChannel, RpcError
from fastapi_canon.integrations.websockets.protocol import RpcProtocol
from pydantic import BaseModel


class GreetingRpcMethod(StrEnum):
    SAY = "greeting.say"
    FORGET = "greeting.forget"
    GREETED = "greeting.greeted"
    CLEAR = "greeting.clear"


class GreetingNotificationMethod(StrEnum):
    CHANGED = "greeting.changed"


class SayParams(BaseModel):
    name: str


class SayResult(BaseModel):
    text: str


class ForgetParams(BaseModel):
    name: str


class GreetedResult(BaseModel):
    names: list[str]


class GreetingSaid(BaseModel):
    type: Literal["greeting.said"] = "greeting.said"
    text: str


class GreetingForgotten(BaseModel):
    type: Literal["greeting.forgotten"] = "greeting.forgotten"
    name: str


type GreetingUpdate = GreetingSaid | GreetingForgotten


class UnknownGreetingError(RpcError):
    rpc_code = -32001
    message = "Unknown greeting"


greeting_channel = RpcChannel("greeting")


class GreetingState:
    def __init__(self) -> None:
        self.greeted: list[str] = []


@greeting_channel.method("say", summary="Greet someone by name.")
async def say(
    params: SayParams,
    state: Inject[GreetingState],
) -> SayResult:
    state.greeted.append(params.name)
    return SayResult(text=f"Hello, {params.name}!")


@greeting_channel.method("forget", raises=(UnknownGreetingError,))
async def forget(
    params: ForgetParams,
    state: Inject[GreetingState],
) -> None:
    """Forget a greeted name."""
    if params.name not in state.greeted:
        raise UnknownGreetingError(message=f"Unknown greeting: {params.name}")
    state.greeted.remove(params.name)


@greeting_channel.method("greeted")
async def greeted_names(
    state: Inject[GreetingState],
) -> GreetedResult:
    """List everyone greeted so far."""
    return GreetedResult(names=list(state.greeted))


@greeting_channel.method("clear")
async def clear(state: Inject[GreetingState]) -> None:
    """Forget everyone."""
    state.greeted.clear()


@greeting_channel.event(
    "changed",
    payload=GreetingUpdate,
    summary="Publish a greeting change.",
)
async def greeting_changed() -> AsyncIterator[GreetingUpdate]:
    if False:
        yield GreetingSaid(text="")


greeting_protocol = greeting_channel.protocol
greeting_app = greeting_channel


@pytest.fixture
def protocol() -> RpcProtocol:
    return greeting_protocol


@pytest.fixture
def handler() -> GreetingState:
    return GreetingState()


class TestResolver:
    __test__ = False

    def __init__(self, *values: object) -> None:
        self.values = {type(value): value for value in values}

    async def resolve[DependencyT](
        self,
        dependency: type[DependencyT],
    ) -> DependencyT:
        return self.values[dependency]  # type: ignore[return-value]
