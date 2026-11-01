from typing import Literal

from fastapi import FastAPI
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcService,
    RpcSocket,
    create_router,
)
from pydantic import BaseModel, ConfigDict


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ping"] = "ping"
    value: int


class Pong(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["pong"] = "pong"
    value: int


messages = RpcChannel("messages")
type PingSocket = RpcSocket[Ping, Pong]


@messages.method()
async def health() -> bool:
    return True


@messages.socket("ping")
async def ping(message: Ping) -> Pong:
    return Pong(value=message.value + 1)


@messages.socket("session")
async def session(socket: Inject[PingSocket]) -> None:
    await socket.send(Pong(value=0))
    async for message in socket:
        await socket.send(Pong(value=message.value))
        await socket.send(Pong(value=message.value + 1))


service = RpcService()
service.socket("/rpc", channels=(messages,))
service.socket("/messages/ping", ping)
service.socket("/messages/session", session)
contract = service.contract(title="Typed Socket", base_url="ws://{host}")
app = FastAPI()
app.include_router(create_router(service))
