import json

from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.protocol import RpcProtocol
from fastapi_canon.integrations.websockets.schema import render_openrpc

from .conftest import GreetingNotificationMethod, GreetingRpcMethod


def test_contract_exports_cancellation_and_only_explicit_method_deadlines():
    channel = RpcChannel("deadlines")

    @channel.method()
    async def inherited() -> None: ...

    @channel.method(timeout=None)
    async def unbounded() -> None: ...

    @channel.method(timeout=5)
    async def bounded() -> None: ...

    app = RpcService()
    app.socket("/rpc", channels=[channel])
    document = app.contract(title="Deadlines", base_url="ws://localhost").to_openrpc()
    methods = {method["name"]: method for method in document["methods"]}
    assert "x-rpc-request-timeout" not in methods["deadlines.inherited"]
    assert methods["deadlines.unbounded"]["x-rpc-request-timeout"] is None
    assert methods["deadlines.bounded"]["x-rpc-request-timeout"] == 5
    control = document["x-rpc-request-cancellation"]
    assert control["method"] == "$/cancelRequest"
    assert control["notification"] is True
    assert control["params"]["properties"]["id"]["type"] == ["integer", "string"]
    assert control["error"] == {"code": -32800, "x-rpckit-code": "request_cancelled"}


def test_openrpc_request_components_pin_the_method_name(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting Protocol")
    request = document["components"]["schemas"]["SayRequest"]

    assert request["properties"]["method"]["const"] == GreetingRpcMethod.SAY
    assert request["properties"]["params"] == {"$ref": "#/components/schemas/SayParams"}
    assert request["required"] == ["jsonrpc", "method", "params"]


def test_params_are_optional_when_the_model_has_no_required_fields(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting Protocol")
    schemas = document["components"]["schemas"]

    assert schemas["SayParams"]["required"] == ["name"]
    assert "params" in schemas["SayRequest"]["required"]


def test_notifications_and_their_types_are_indexed_as_extensions(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting Protocol")

    assert document["x-rpc-protocol-version"] == protocol.version
    assert [item["name"] for item in document["x-rpc-notification-types"]] == [
        "greeting.said",
        "greeting.forgotten",
    ]
    notification = document["x-rpc-notifications"][0]
    assert notification["name"] == GreetingNotificationMethod.CHANGED
    assert notification["message"] == {
        "$ref": "#/components/schemas/GreetingChangedNotification"
    }


def test_openrpc_describes_methods_by_name(protocol: RpcProtocol) -> None:
    document = render_openrpc(protocol, title="Greeting")
    say = next(
        method
        for method in document["methods"]
        if method["name"] == GreetingRpcMethod.SAY
    )

    assert say["summary"] == "Greet someone by name."
    assert say["paramStructure"] == "by-name"
    assert say["params"] == [
        {
            "name": "name",
            "required": True,
            "schema": {"title": "Name", "type": "string"},
        }
    ]
    assert say["result"]["schema"] == {"$ref": "#/components/schemas/SayResult"}


def test_openrpc_documents_the_declared_errors(protocol: RpcProtocol) -> None:
    document = render_openrpc(protocol, title="Greeting")
    say = next(
        method
        for method in document["methods"]
        if method["name"] == GreetingRpcMethod.SAY
    )
    forget = next(
        method
        for method in document["methods"]
        if method["name"] == GreetingRpcMethod.FORGET
    )

    assert "errors" not in say
    assert forget["errors"] == [
        {
            "code": -32001,
            "message": "Unknown greeting",
            "x-rpckit-code": "unknown_greeting",
        }
    ]


def test_openrpc_does_not_add_tags(protocol: RpcProtocol) -> None:
    document = render_openrpc(protocol, title="Greeting")
    say = next(
        method
        for method in document["methods"]
        if method["name"] == GreetingRpcMethod.SAY
    )

    assert "tags" not in say


def test_openrpc_takes_a_missing_summary_from_the_docstring(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting")
    forget = next(
        method
        for method in document["methods"]
        if method["name"] == GreetingRpcMethod.FORGET
    )

    assert forget["summary"] == "Forget a greeted name."


def test_openrpc_rewrites_every_reference_into_components(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(
        protocol,
        title="Greeting",
        servers=({"name": "local", "url": "ws://127.0.0.1:8000/rpc"},),
    )

    assert "#/$defs/" not in json.dumps(document)
    assert document["servers"] == [{"name": "local", "url": "ws://127.0.0.1:8000/rpc"}]
    assert "SayParams" in document["components"]["schemas"]


def test_a_request_without_params_accepts_an_omitted_or_empty_member(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting Protocol")
    request = document["components"]["schemas"]["ClearRequest"]

    assert request["required"] == ["jsonrpc", "method"]
    assert request["properties"]["params"] == {
        "additionalProperties": False,
        "type": "object",
    }


def test_openrpc_describes_a_method_without_params_as_taking_none(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting")
    greeted = _method(document, GreetingRpcMethod.GREETED)

    assert greeted["params"] == []
    assert "x-rpc-params-schema" not in greeted
    assert greeted["result"]["schema"] == {"$ref": "#/components/schemas/GreetedResult"}


def test_openrpc_still_names_the_request_schema_of_a_method_without_params(
    protocol: RpcProtocol,
) -> None:
    document = render_openrpc(protocol, title="Greeting")

    assert _method(document, GreetingRpcMethod.CLEAR)["x-rpc-request-schema"] == {
        "$ref": "#/components/schemas/ClearRequest"
    }


def _method(document: dict[str, object], name: str) -> dict[str, object]:
    methods: list[dict[str, object]] = document["methods"]
    return next(method for method in methods if method["name"] == name)
