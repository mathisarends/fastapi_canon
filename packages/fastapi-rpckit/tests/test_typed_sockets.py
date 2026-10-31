import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcChannel,
    RpcConnectionClose,
    RpcErrorBinding,
    RpcMessageType,
    RpcRejection,
    RpcService,
    RpcSocketProtocol,
)
from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .testing import RpcTestClient


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ping"] = "ping"
    value: int = Field(ge=0)


class Pong(BaseModel):
    type: Literal["pong"] = "pong"
    value: int


channel = RpcChannel("messages")


@channel.socket("ping")
async def ping(message: Ping) -> Pong:
    assert isinstance(message, Ping)
    return Pong(value=message.value + 1)


def service() -> RpcService:
    result = RpcService()
    result.socket("/ping", ping)
    return result


async def test_typed_messages_are_validated_and_serialized() -> None:
    async with RpcTestClient(service(), "/ping") as client:
        await client.send_message('{"type":"ping","value":3}')
        assert await client.next_message() == '{"type":"pong","value":4}'


class Offset:
    value = 10


async def test_message_calls_are_sequential_with_paths_and_invocation_scopes() -> None:
    entered = []
    released = []
    calls = []
    started = asyncio.Event()
    proceed = asyncio.Event()

    @asynccontextmanager
    async def scope(resolver):
        invocation = len(entered)
        entered.append(invocation)
        try:
            yield resolver
        finally:
            released.append(invocation)

    channel = RpcChannel("scoped", resolver_scope=scope)

    @channel.socket()
    async def handle(payload: Ping, multiplier: int, offset: Inject[Offset]) -> Pong:
        calls.append(payload.value)
        if payload.value == 1:
            started.set()
            await proceed.wait()
        return Pong(value=payload.value * multiplier + offset.value)

    app = RpcService()
    app.socket("/messages/{multiplier}", handle)
    async with RpcTestClient(app, "/messages/3", context=Offset()) as client:
        await client.send_message('{"value":1}')
        await asyncio.wait_for(started.wait(), 1)
        await client.send_message('{"value":2}')
        assert entered == [0] and calls == [1] and not released
        proceed.set()
        assert json.loads(await client.next_message())["value"] == 13
        assert json.loads(await client.next_message())["value"] == 16
        await client.send_message('{"value":"bad"}')
        assert (await client.closed())[0] == RpcConnectionClose.PROTOCOL_ERROR
    assert calls == [1, 2]
    assert entered == released == [0, 1]


async def test_disconnect_cancels_a_message_call_and_releases_its_scope() -> None:
    started = asyncio.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def scope(resolver):
        try:
            yield resolver
        finally:
            released.set()

    channel = RpcChannel("cancel", resolver_scope=scope)

    @channel.socket()
    async def handle(message: Ping) -> Pong:
        started.set()
        await asyncio.Future()
        return Pong(value=0)

    app = RpcService()
    app.socket("/cancel", handle)
    async with RpcTestClient(app, "/cancel") as client:
        await client.send_message('{"value":1}')
        await asyncio.wait_for(started.wait(), 1)
        await client.socket.client_disconnect(1001)
        await asyncio.wait_for(released.wait(), 1)
        assert await client.closed() is None


async def test_message_handler_exceptions_use_declared_close_bindings() -> None:
    class Denied(Exception):
        pass

    channel = RpcChannel("rejections")

    @channel.socket(
        closes=(
            RpcErrorBinding(Denied, rejection=RpcRejection.FORBIDDEN, message="Denied"),
        )
    )
    async def handle(message: Ping) -> Pong:
        raise Denied("secret")

    app = RpcService()
    app.socket("/denied", handle)
    async with RpcTestClient(app, "/denied") as client:
        await client.send_message('{"value":1}')
        assert await client.closed() == (RpcConnectionClose.POLICY_VIOLATION, "Denied")


async def test_invalid_queued_input_cancels_an_active_invocation_and_its_scope() -> (
    None
):
    started = asyncio.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def scope(resolver):
        try:
            yield resolver
        finally:
            released.set()

    channel = RpcChannel("invalid", resolver_scope=scope)

    @channel.socket()
    async def handle(message: Ping) -> Pong:
        started.set()
        await asyncio.Future()
        return Pong(value=0)

    app = RpcService()
    app.socket("/invalid", handle)
    async with RpcTestClient(app, "/invalid") as client:
        await client.send_message('{"value":1}')
        await asyncio.wait_for(started.wait(), 1)
        await client.send_message('{"value":"secret"}')
        assert await client.closed() == (
            RpcConnectionClose.PROTOCOL_ERROR,
            "Invalid incoming socket message",
        )
        await asyncio.wait_for(released.wait(), 1)


async def test_none_response_is_json_null() -> None:
    channel = RpcChannel("null")

    @channel.socket()
    async def handle(message: Ping) -> None:
        pass

    app = RpcService()
    app.socket("/null", handle)
    async with RpcTestClient(app, "/null") as client:
        await client.send_message('{"value":1}')
        assert await client.next_message() == "null"


@pytest.mark.parametrize(
    "message",
    [
        "not JSON",
        '{"value":"3"}',
        '{"value":-1}',
        '{"value":3,"secret":true}',
        '{"type":"other","value":3}',
        b'{"value":3}',
    ],
)
async def test_invalid_messages_close_without_reaching_the_handler(message) -> None:
    async with RpcTestClient(service(), "/ping") as client:
        await client.send_message(message)
        code, reason = await client.closed()
        assert code == RpcConnectionClose.PROTOCOL_ERROR
        assert "secret" not in reason


async def test_invalid_outgoing_models_cannot_bypass_validation() -> None:
    broken = RpcChannel("broken")

    @broken.socket()
    async def invalid(message: Ping) -> Pong:
        response = Pong(value=1)
        response.value = "secret"  # type: ignore[assignment]
        return response

    app = RpcService()
    app.socket("/broken", invalid)
    async with RpcTestClient(app, "/broken") as client:
        await client.send_message('{"value":1}')
        assert await client.closed() == (
            RpcConnectionClose.INTERNAL_ERROR,
            "Invalid outgoing socket message",
        )
        assert client.socket.accepted


def test_contract_has_directional_schemas() -> None:
    document = (
        service().contract(title="Messages", base_url="ws://localhost").to_openrpc()
    )
    endpoint = document["x-rpckit-sockets"][0]
    assert endpoint["messageTypes"] == ["text"]
    assert endpoint["messages"] == {
        "client": {"$ref": "#/components/schemas/MessagesPingClientMessage"},
        "server": {"$ref": "#/components/schemas/MessagesPingServerMessage"},
    }
    assert "Ping" in document["components"]["schemas"]


type Incoming = Annotated[Ping | Pong, Field(discriminator="type")]


async def test_discriminated_unions_are_preserved() -> None:
    union = RpcChannel("union")
    received = []

    @union.socket()
    async def handle(message: Incoming) -> Pong:
        received.append(message)
        return Pong(value=5)

    app = RpcService()
    app.socket("/union", handle)
    async with RpcTestClient(app, "/union") as client:
        await client.send_message('{"type":"pong","value":4}')
        assert await client.next_message() == '{"type":"pong","value":5}'
    assert isinstance(received[0], Pong)


class AliasedOutput(BaseModel):
    value: int = Field(serialization_alias="wireValue")

    @field_serializer("value")
    def as_text(self, value: int) -> str:
        return str(value)


async def test_serialization_aliases_and_serializers_define_the_output() -> None:
    channel = RpcChannel("aliases")

    @channel.socket()
    async def handle(message: Ping) -> AliasedOutput:
        return AliasedOutput(value=3)

    app = RpcService()
    app.socket("/aliases", handle)
    document = app.contract(title="Aliases", base_url="ws://h").to_openrpc()
    assert (
        document["components"]["schemas"]["AliasedOutput"]["properties"]["wireValue"][
            "type"
        ]
        == "string"
    )
    async with RpcTestClient(app, "/aliases") as client:
        await client.send_message('{"value":1}')
        assert await client.next_message() == '{"wireValue":"3"}'


def test_typed_sockets_reject_incompatible_framing_and_encoding() -> None:
    with pytest.raises(ProtocolDefinitionError, match="text"):

        @RpcChannel("binary").socket(messages=(RpcMessageType.BINARY,))
        async def binary(message: Ping) -> Pong: ...

    with pytest.raises(ProtocolDefinitionError, match="JSON"):

        @RpcChannel("encoding").socket(protocol=RpcSocketProtocol("x", encoding="cbor"))
        async def cbor(message: Ping) -> Pong: ...


def test_message_handler_annotations_and_parameters_are_required() -> None:
    with pytest.raises(ProtocolDefinitionError, match="concrete"):

        @RpcChannel("unknown").socket()
        async def unknown(message: Any) -> Pong: ...

    with pytest.raises(ProtocolDefinitionError, match="return annotations"):

        @RpcChannel("missing").socket()
        async def missing(message: Ping): ...

    with pytest.raises(ProtocolDefinitionError, match="required"):

        @RpcChannel("default").socket()
        async def default(message: int = 0) -> int:
            return message

    with pytest.raises(ProtocolDefinitionError, match="passable by name"):

        @RpcChannel("positional").socket()
        async def positional(message: Ping, /) -> Pong: ...

    with pytest.raises(ProtocolDefinitionError, match="also be a path variable"):
        RpcService().socket("/messages/{message}", ping)


def test_message_handler_signature_preserves_both_directions(tmp_path: Path) -> None:
    source = tmp_path / "typing_example.py"
    source.write_text(
        """
from typing import Literal, assert_type
from pydantic import BaseModel
from fastapi_canon.integrations.websockets import RpcChannel

class Ping(BaseModel):
    type: Literal["ping"] = "ping"

class Pong(BaseModel):
    type: Literal["pong"] = "pong"

channel = RpcChannel("messages")

@channel.socket()
async def handler(message: Ping) -> Pong:
    assert_type(message, Ping)
    return Ping()
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
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    errors = [item for item in diagnostics if item["severity"] == "error"]
    assert len(errors) == 1, diagnostics
    assert errors[0]["rule"] == "reportReturnType"
    assert "Ping" in errors[0]["message"] and "Pong" in errors[0]["message"]
