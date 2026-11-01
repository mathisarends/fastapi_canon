import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi_canon.integrations.websockets import RpcChannel, RpcMiddleware
from fastapi_canon.integrations.websockets.codegen.ir import build_ir
from fastapi_canon.integrations.websockets.envelopes import RpcRequestEnvelope
from fastapi_canon.integrations.websockets.request_metadata import (
    REQUEST_METADATA_CAPABILITY,
    validate_request_metadata,
)
from pydantic import BaseModel, ValidationError


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {"other": "x"},
        {"traceparent": 1},
        {"baggage": "x" * 4096},
        {"baggage": "\ud800"},
        {"baggage": "é" * 2048},
    ],
)
async def test_invalid_metadata_is_rejected_before_handler_or_scope_adoption(metadata):
    calls, observed = [], []
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> str:
        calls.append(True)
        return "pong"

    class Observe(RpcMiddleware):
        @asynccontextmanager
        async def request(self, scope):
            observed.append(scope.metadata)
            yield

    server = channel.create_server(middleware=[Observe()])
    result = await server.handle(
        {"jsonrpc": "2.0", "method": "demo.ping", "id": 1, "meta": metadata}
    )
    assert result.error.code == -32600
    assert calls == [] and observed == [None]


async def test_metadata_is_immutable_independent_of_named_params_and_batch_members():
    observed = []
    channel = RpcChannel("demo")

    class Params(BaseModel):
        name: str

    @channel.method()
    async def greet(params: Params) -> str:
        await asyncio.sleep(0)
        return params.name

    class Observe(RpcMiddleware):
        @asynccontextmanager
        async def request(self, scope):
            observed.append(scope.metadata)
            with pytest.raises(TypeError):
                scope.metadata["traceparent"] = "changed"
            yield

    server = channel.create_server(middleware=[Observe()])
    requests = [
        {
            "jsonrpc": "2.0",
            "method": "demo.greet",
            "id": i,
            "params": {"name": str(i)},
            "meta": {"traceparent": str(i)},
        }
        for i in (1, 2)
    ]
    results = await server.handle(requests)
    assert [result.result for result in results] == ["1", "2"]
    assert {item["traceparent"] for item in observed} == {"1", "2"}
    assert requests[0]["params"] == {"name": "1"}


def test_metadata_wire_schema_and_exact_capability_negotiation():
    channel = RpcChannel("demo")

    @channel.method()
    async def ping() -> None:
        pass

    from fastapi_canon.integrations.websockets.schema import render_openrpc

    document = render_openrpc(channel.protocol, title="Demo")
    assert document["x-rpc-request-metadata"] == REQUEST_METADATA_CAPABILITY
    assert build_ir(document).request_metadata
    document["x-rpc-request-metadata"]["allowedKeys"].append("other")
    assert (
        render_openrpc(channel.protocol, title="Demo")["x-rpc-request-metadata"]
        == REQUEST_METADATA_CAPABILITY
    )
    schema = next(
        item
        for item in document["components"]["schemas"].values()
        if item.get("properties", {}).get("method", {}).get("const") == "demo.ping"
    )
    assert "meta" in schema["properties"] and "meta" not in schema["required"]
    document.pop("x-rpc-request-metadata")
    assert not build_ir(document).request_metadata
    document["x-rpc-request-metadata"] = {**REQUEST_METADATA_CAPABILITY, "version": 2}
    assert not build_ir(document).request_metadata
    document["x-rpc-request-metadata"] = {
        **REQUEST_METADATA_CAPABILITY,
        "version": True,
    }
    assert not build_ir(document).request_metadata


def test_metadata_bound_counts_compact_utf8_and_envelope_keeps_params_separate():
    # 14 bytes of JSON syntax around the baggage value.
    assert validate_request_metadata({"baggage": "x" * 4082})
    with pytest.raises(ValueError):
        validate_request_metadata({"baggage": "x" * 4083})
    envelope = RpcRequestEnvelope.model_validate(
        {"jsonrpc": "2.0", "method": "ping", "id": 1, "meta": {}}
    )
    assert envelope.params == {} and envelope.meta == {}
    with pytest.raises(ValidationError):
        RpcRequestEnvelope.model_validate(
            {"jsonrpc": "2.0", "method": "ping", "meta": None}
        )
