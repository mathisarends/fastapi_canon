from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import build_ir
from fastapi_canon.integrations.websockets.codegen.ir import (
    LiteralType,
    ModelDecl,
    NamedType,
    Primitive,
    PrimitiveType,
    SocketProtocolDecl,
    UnionType,
    UnsupportedSchemaError,
    type_expression,
)


def test_methods_form_a_hierarchical_api_tree(document: dict[str, Any]) -> None:
    ir = build_ir(document)

    assert [node.path for node in ir.api] == [("greeting",)]
    assert [route.operation_name for route in ir.api[0].operations] == [
        "say",
        "forget",
        "greeted",
        "clear",
    ]
    assert ir.root_operations == ()


def test_an_operation_keeps_its_wire_name_and_models(document: dict[str, Any]) -> None:
    ir = build_ir(document)
    say = ir.operations[0]

    assert say.rpc_name == "greeting.say"
    assert say.path == ("greeting",)
    assert say.operation_name == "say"
    assert say.method_member == "GREETING_SAY"
    assert say.params_model == "SayParams"
    assert say.result == NamedType("SayResult")
    assert say.summary == "Greet someone by name."
    assert [parameter.name for parameter in say.params] == ["name"]


def test_a_method_without_a_result_lowers_to_null(document: dict[str, Any]) -> None:
    ir = build_ir(document)
    forget = ir.operations[1]

    assert forget.result == PrimitiveType(Primitive.NULL)


def test_envelope_schemas_are_pruned(document: dict[str, Any]) -> None:
    names = {declaration.name for declaration in build_ir(document).declarations}

    assert "SayRequest" not in names
    assert "RpcSuccess" not in names
    assert "RpcFailure" not in names


def test_types_reachable_from_the_api_surface_are_kept(
    document: dict[str, Any],
) -> None:
    names = {declaration.name for declaration in build_ir(document).declarations}

    assert {"SayParams", "SayResult", "ForgetParams"} <= names
    assert {"GreetingChangedNotification", "GreetingUpdate"} <= names
    assert {"GreetingSaid", "GreetingForgotten"} <= names


def test_models_carry_their_fields(document: dict[str, Any]) -> None:
    ir = build_ir(document)
    say_params = next(
        declaration
        for declaration in ir.declarations
        if isinstance(declaration, ModelDecl) and declaration.name == "SayParams"
    )

    assert [(f.name, f.required) for f in say_params.fields] == [("name", True)]


def test_notifications_are_lowered(document: dict[str, Any]) -> None:
    ir = build_ir(document)

    assert len(ir.notifications) == 1
    assert ir.notifications[0].rpc_name == "greeting.changed"
    assert ir.notifications[0].message == NamedType("GreetingChangedNotification")
    assert ir.notifications[0].payload == NamedType("GreetingUpdate")


def test_notification_server_metadata_is_preserved(
    document: dict[str, Any],
) -> None:
    deployed = deepcopy(document)
    deployed["x-rpc-notifications"][0]["servers"] = [
        {"name": "updates", "url": "wss://example.com/updates"}
    ]
    deployed["servers"] = [{"name": "updates", "url": "wss://example.com/updates"}]

    notification = build_ir(deployed).notifications[0]

    assert notification.server == "updates"


def test_nested_routes_form_nested_api_nodes(document: dict[str, Any]) -> None:
    nested = deepcopy(document)
    route = deepcopy(nested["methods"][0])
    route["name"] = "browser.nav.navigate"
    nested["methods"] = [route]

    ir = build_ir(nested)

    assert ir.api[0].path == ("browser",)
    assert ir.api[0].children[0].path == ("browser", "nav")
    assert ir.api[0].children[0].operations[0].operation_name == "navigate"


def test_route_metadata_is_preserved(document: dict[str, Any]) -> None:
    enriched = deepcopy(document)
    method = enriched["methods"][0]
    method.update(
        {
            "description": "A longer explanation.",
            "deprecated": True,
            "servers": [{"name": "secondary", "url": "wss://secondary"}],
            "errors": [
                {
                    "code": -32004,
                    "message": "Missing",
                    "x-rpckit-code": "greeting_missing",
                    "x-rpckit-details-schema": {
                        "$ref": "#/components/schemas/MissingData"
                    },
                }
            ],
        }
    )
    enriched["components"]["schemas"]["MissingData"] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    enriched["servers"] = [{"name": "secondary", "url": "wss://secondary"}]

    route = build_ir(enriched).operations[0]

    assert route.description == "A longer explanation."
    assert route.deprecated is True
    assert route.tags == ()
    assert route.server == "secondary"
    assert route.errors[0].code == "greeting_missing"
    assert route.errors[0].data == NamedType("MissingData")


def test_contract_servers_are_lowered(document: dict[str, Any]) -> None:
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "greeting-api",
            "url": "wss://{host}/{tenantId}",
            "summary": "Production gateway",
            "variables": {
                "host": {"default": "api.example.com"},
                "tenantId": {
                    "default": "demo",
                    "enum": ["demo", "production"],
                },
            },
            "x-rpckit-transport": {
                "type": "websocket",
                "messageEncoding": "json",
            },
        }
    ]

    ir = build_ir(deployed)

    assert ir.protocol_version == 1
    assert ir.servers[0].name == "greeting-api"
    assert ir.servers[0].transport is not None
    assert ir.servers[0].transport.type == "websocket"
    assert ir.servers[0].transport.message_encoding == "json"
    assert [variable.name for variable in ir.servers[0].variables] == [
        "host",
        "tenantId",
    ]


def test_binary_stream_extensions_are_lowered(document: dict[str, Any]) -> None:
    deployed = deepcopy(document)
    deployed["x-rpckit-binary-streams"] = [
        {
            "name": "voice",
            "url": "wss://media/{sessionId}",
            "direction": "server-to-client",
            "contentType": "audio/pcm;rate=24000",
            "frameType": "binary",
            "variables": {"sessionId": {"default": "demo"}},
            "subprotocols": ["voice.v1"],
        }
    ]

    stream = build_ir(deployed).binary_streams[0]

    assert stream.name == "voice"
    assert stream.direction == "server-to-client"
    assert stream.content_type == "audio/pcm;rate=24000"
    assert stream.variables[0].name == "sessionId"
    assert stream.subprotocols == ("voice.v1",)


def test_a_foreign_openrpc_document_is_rejected(document: dict[str, Any]) -> None:
    foreign = {**document, "methods": [{"name": "a.b", "params": [], "result": {}}]}

    with pytest.raises(UnsupportedSchemaError, match="x-rpc-request-schema"):
        build_ir(foreign)


def test_an_operation_without_params_carries_no_params_model(
    document: dict[str, Any],
) -> None:
    ir = build_ir(document)
    greeted = ir.operations[2]

    assert greeted.params == ()
    assert greeted.params_model is None
    assert greeted.result == NamedType("GreetedResult")


def test_inline_enums_become_literal_unions() -> None:
    assert type_expression({"type": "string", "enum": ["left", "right"]}) == (
        UnionType((LiteralType("left"), LiteralType("right")))
    )


def test_documents_with_client_methods_are_rejected(
    document: dict[str, Any],
) -> None:
    legacy = {**document, "x-rpc-client-methods": []}

    with pytest.raises(UnsupportedSchemaError, match="x-rpc-client-methods"):
        build_ir(legacy)


def test_contracts_with_the_renamed_raw_socket_key_are_rejected(
    document: dict[str, Any],
) -> None:
    legacy = {
        **document,
        "x-rpckit-raw-sockets": [{"name": "browser.cdp", "url": "wss://api/cdp"}],
    }

    with pytest.raises(UnsupportedSchemaError, match="renamed to x-rpckit-sockets"):
        build_ir(legacy)


def _with_socket(document: dict[str, Any], protocol: object) -> dict[str, Any]:
    return {
        **document,
        "x-rpckit-sockets": [
            {
                "name": "browser.cdp",
                "url": "wss://api/cdp",
                "x-rpckit-protocol": protocol,
            }
        ],
    }


def test_socket_protocols_are_kept_in_the_ir(document: dict[str, Any]) -> None:
    ir = build_ir(
        _with_socket(
            document,
            {
                "name": "cdp",
                "version": "1.3",
                "encoding": "json",
                "schemas": ["schemas/browser.json", "https://cdn/js.json"],
            },
        )
    )

    assert ir.sockets[0].protocol == SocketProtocolDecl(
        name="cdp",
        encoding="json",
        version="1.3",
        schemas=("schemas/browser.json", "https://cdn/js.json"),
    )
    assert ir.api[-1].sockets[0].protocol == ir.sockets[0].protocol


def test_sockets_without_a_protocol_keep_none(document: dict[str, Any]) -> None:
    ir = build_ir(
        {
            **document,
            "x-rpckit-sockets": [{"name": "terminal", "url": "wss://api/terminal"}],
        }
    )

    assert ir.sockets[0].protocol is None


@pytest.mark.parametrize(
    ("protocol", "message"),
    [
        ("cdp", "must be an object"),
        ({"name": "CDP"}, "lowercase token"),
        ({"name": "cdp", "encoding": ""}, "encoding"),
        ({"name": "cdp", "version": 1}, "version"),
        ({"name": "cdp", "schemas": "a.json"}, "URI references"),
        ({"name": "cdp", "schemas": ["a b"]}, "URI references"),
    ],
)
def test_invalid_socket_protocols_are_rejected(
    document: dict[str, Any], protocol: object, message: str
) -> None:
    with pytest.raises(UnsupportedSchemaError, match=message):
        build_ir(_with_socket(document, protocol))


def test_relative_protocol_schemas_resolve_against_the_contract(
    tmp_path: Path,
) -> None:
    protocol = SocketProtocolDecl(
        name="cdp", schemas=("schemas/browser.json", "https://cdn/js.json")
    )
    contract = tmp_path / "contracts" / "browser.openrpc.json"

    assert protocol.resolve_schemas(None) == protocol.schemas
    assert protocol.resolve_schemas("https://api.example.com/v1/openrpc.json") == (
        "https://api.example.com/v1/schemas/browser.json",
        "https://cdn/js.json",
    )
    assert protocol.resolve_schemas(contract) == (
        (tmp_path / "contracts" / "schemas" / "browser.json").resolve().as_uri(),
        "https://cdn/js.json",
    )
    assert protocol.resolve_schemas(str(contract)) == protocol.resolve_schemas(contract)
