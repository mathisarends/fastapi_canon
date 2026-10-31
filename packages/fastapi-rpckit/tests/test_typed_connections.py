import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcChannel,
    RpcConnectionClose,
    RpcMessageType,
    RpcService,
    RpcSocket,
    RpcSocketProtocol,
)
from pydantic import BaseModel

from .test_typed_sockets import Ping, Pong
from .testing import RpcTestClient


class Ready(BaseModel):
    type: Literal["ready"] = "ready"


type Output = Pong | Ready
type SessionSocket = RpcSocket[Ping, Output]

channel = RpcChannel("sessions")


@channel.socket()
async def session(socket: Inject[SessionSocket]) -> None:
    await socket.send(Ready())
    async for message in socket:
        await socket.send(Pong(value=message.value))
        await socket.send(Pong(value=message.value + 1))


def service() -> RpcService:
    app = RpcService()
    app.socket("/session", session)
    return app


async def test_typed_connection_sends_unsolicited_events_and_multiple_replies() -> None:
    async with RpcTestClient(service(), "/session") as client:
        assert await client.next_message() == '{"type":"ready"}'
        for value in (4, 7):
            await client.send_message(json.dumps({"value": value}))
            assert json.loads(await client.next_message())["value"] == value
            assert json.loads(await client.next_message())["value"] == value + 1


@pytest.mark.parametrize(
    "message", ["not JSON", '{"value":"secret"}', '{"value":1,"secret":true}', b"{}"]
)
async def test_typed_connection_rejects_invalid_incoming_messages(message) -> None:
    async with RpcTestClient(service(), "/session") as client:
        assert await client.next_message() == '{"type":"ready"}'
        await client.send_message(message)
        code, reason = await client.closed()
        assert code == RpcConnectionClose.PROTOCOL_ERROR
        assert "secret" not in reason


async def test_typed_connection_revalidates_mutated_output_and_releases_handler() -> (
    None
):
    released = asyncio.Event()
    local = RpcChannel("invalid")

    @local.socket()
    async def invalid(socket: Inject[RpcSocket[Ping, Pong]]) -> None:
        try:
            output = Pong(value=1)
            output.value = "secret"  # type: ignore[assignment]
            await socket.send(output)
        finally:
            released.set()

    app = RpcService()
    app.socket("/invalid", invalid)
    async with RpcTestClient(app, "/invalid") as client:
        assert await client.closed() == (
            RpcConnectionClose.INTERNAL_ERROR,
            "Invalid outgoing socket message",
        )
        await asyncio.wait_for(released.wait(), 1)


async def test_typed_connection_keeps_one_scope_and_releases_it_on_disconnect() -> None:
    entered = []
    released = asyncio.Event()
    cancelled = asyncio.Event()

    class Offset:
        value = 10

    @asynccontextmanager
    async def scope(resolver):
        entered.append(True)
        try:
            yield resolver
        finally:
            released.set()

    local = RpcChannel("scoped", resolver_scope=scope)

    @local.socket()
    async def scoped(
        socket: Inject[RpcSocket[Ping, Pong]], multiplier: int, offset: Inject[Offset]
    ) -> None:
        try:
            async for message in socket:
                await socket.send(Pong(value=message.value * multiplier + offset.value))
        finally:
            cancelled.set()

    app = RpcService()
    app.socket("/scoped/{multiplier}", scoped)
    async with RpcTestClient(app, "/scoped/3", context=Offset()) as client:
        for value in (1, 2):
            await client.send_message(json.dumps({"value": value}))
            assert json.loads(await client.next_message())["value"] == value * 3 + 10
        assert entered == [True] and not released.is_set()
        await client.socket.client_disconnect(1001)
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(released.wait(), 1)
        assert await client.closed() is None


def test_typed_connection_exports_directional_schemas() -> None:
    document = service().contract(title="Session", base_url="ws://h").to_openrpc()
    endpoint = document["x-rpckit-sockets"][0]
    assert endpoint["messageTypes"] == ["text"]
    assert "codec" not in endpoint
    assert endpoint["messages"] == {
        "client": {"$ref": "#/components/schemas/SessionsSessionClientMessage"},
        "server": {"$ref": "#/components/schemas/SessionsSessionServerMessage"},
    }
    schemas = document["components"]["schemas"]
    assert schemas["SessionsSessionClientMessage"] == {
        "$ref": "#/components/schemas/Ping"
    }
    assert schemas["Output"]["anyOf"] == [
        {"$ref": "#/components/schemas/Pong"},
        {"$ref": "#/components/schemas/Ready"},
    ]


def test_typed_connections_require_concrete_types_and_json_text() -> None:
    with pytest.raises(ProtocolDefinitionError, match="concrete"):

        @RpcChannel("unknown").socket()
        async def unknown(socket: Inject[RpcSocket[Any, Pong]]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="concrete"):

        @RpcChannel("unknown").socket()
        async def unknown_output(socket: Inject[RpcSocket[Ping, Any]]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="text"):

        @RpcChannel("binary").socket(messages=(RpcMessageType.BINARY,))
        async def binary(socket: Inject[SessionSocket]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="encoding"):

        @RpcChannel("encoding").socket(protocol=RpcSocketProtocol("x", encoding="cbor"))
        async def cbor(socket: Inject[SessionSocket]) -> None: ...


def test_connection_annotations_preserve_ide_types_and_reject_wrong_sends(
    tmp_path: Path,
) -> None:
    source = tmp_path / "consumer.py"
    source.write_text(
        """
from typing import assert_type
from pydantic import BaseModel
from fastapi_canon.integrations.websockets import Inject, RpcChannel, RpcSocket

class Ping(BaseModel):
    value: int
class Pong(BaseModel):
    text: str

type Session = RpcSocket[Ping, Pong]

@RpcChannel("sessions").socket()
async def session(socket: Inject[Session]) -> None:
    assert_type(await socket.receive(), Ping)
    async for message in socket:
        assert_type(message, Ping)
    await socket.send(Pong(text="ok"))
    await socket.send(Ping(value=1))

async def raw(socket: RpcSocket) -> None:
    assert_type(await socket.receive(), str | bytes)
    await socket.send(b"opaque")
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.12",
            "--outputjson",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    errors = [
        item
        for item in json.loads(result.stdout)["generalDiagnostics"]
        if item["severity"] == "error"
    ]
    assert len(errors) == 1, result.stdout
    assert errors[0]["rule"] == "reportArgumentType"
    assert "Ping" in errors[0]["message"] and "Pong" in errors[0]["message"]
