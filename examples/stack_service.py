from collections.abc import AsyncIterator
from typing import Literal

from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import FastAPI
from fastapi_canon import CanonRouter, Composition, Feature
from fastapi_canon.integrations.sse import CanonStreamRouter
from fastapi_canon.integrations.websockets import Inject, RpcChannel, RpcService
from fastapi_canon.integrations.websockets.adapters.dishka import dishka_router
from pydantic import BaseModel


class Counter:
    def __init__(self) -> None:
        self.value = 0
        self.closed = False
        self.calls_closed = 0


class Operations:
    def __init__(self, counter: Counter) -> None:
        self.counter = counter

    def increment(self) -> int:
        self.counter.value += 1
        return self.counter.value


class StackProvider(Provider):
    @provide(scope=Scope.APP)
    async def counter(self) -> AsyncIterator[Counter]:
        counter = Counter()
        yield counter
        counter.closed = True

    @provide(scope=Scope.REQUEST)
    async def operations(self, counter: Counter) -> AsyncIterator[Operations]:
        yield Operations(counter)
        counter.calls_closed += 1


class CounterEvent(BaseModel):
    type: Literal["counter.changed"] = "counter.changed"
    value: int


class IncrementResult(BaseModel):
    value: int


def create_app() -> FastAPI:
    http = CanonRouter()
    streams = CanonStreamRouter()
    channel = RpcChannel("counter")

    @http.post("/counter", operation_id="increment_counter")
    @inject
    async def increment_http(operations: FromDishka[Operations]) -> IncrementResult:
        return IncrementResult(value=operations.increment())

    @streams.stream("/counter/events", operation_id="counter_events")
    @inject
    async def events(counter: FromDishka[Counter]) -> AsyncIterator[CounterEvent]:
        yield CounterEvent(value=counter.value)

    @channel.method("increment")
    async def increment_rpc(operations: Inject[Operations]) -> IncrementResult:
        return IncrementResult(value=operations.increment())

    rpc = RpcService()
    rpc.socket("/rpc", channels=(channel,))
    feature = Feature(
        name="counter",
        routers=[http, streams, dishka_router(rpc)],
        providers=[StackProvider],
    )
    app = Composition(feature).apply(FastAPI(title="Canon stack example"))
    app.state.rpc_service = rpc
    return app


app = create_app()
