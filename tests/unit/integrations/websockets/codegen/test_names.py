from copy import deepcopy
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import (
    render_python_client,
    render_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.ir import UnsupportedSchemaError
from fastapi_canon.integrations.websockets.codegen.names import (
    camel_case,
    pascal_case,
    snake_case,
)
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)


def test_identifier_styles_handle_acronyms_stably() -> None:
    assert snake_case("parseURL") == "parse_url"
    assert snake_case("HTTPStatus") == "http_status"
    assert camel_case("parseURL") == "parseUrl"
    assert pascal_case("browser-nav") == "BrowserNav"


def test_api_aliases_cannot_merge_distinct_wire_paths(
    document: dict[str, Any],
) -> None:
    collision = deepcopy(document)
    first = collision["methods"][0]
    second = deepcopy(first)
    first["name"] = "browser.nav.tabs.open"
    second["name"] = "browser.navigation.history.close"
    collision["methods"] = [first, second]

    with pytest.raises(UnsupportedSchemaError, match="both map to 'navigation'"):
        render_python_client(
            collision,
            PythonClientOptions(
                package="browser_client",
                api_root="browser",
                api_names={"nav": "navigation"},
            ),
        )


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize(
    "name", ["close", "closed", "state", "states", "droppedNotifications"]
)
def test_root_operations_cannot_shadow_client_lifecycle(
    document: dict[str, Any],
    language,
    name,
) -> None:
    collision = deepcopy(document)
    collision["methods"][0]["name"] = (
        "dropped_notifications"
        if language == "python" and name == "droppedNotifications"
        else name
    )
    collision["methods"] = [collision["methods"][0]]

    with pytest.raises(UnsupportedSchemaError, match="root client"):
        if language == "python":
            render_python_client(collision, PythonClientOptions(package="client"))
        else:
            render_typescript_client(collision, TypeScriptClientOptions())


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize(
    "name",
    [
        "RpcConnectError",
        "RpcConnectionLostError",
        "RpcRequestTimeoutError",
        "RpcConnectionState",
        "RpcNotificationOverflowHandler",
    ],
)
def test_sdk_error_names_cannot_be_shadowed_by_models(language, name):
    document = {
        "openrpc": "1.4.1",
        "info": {"title": "Collision", "version": "1"},
        "components": {
            "schemas": {
                name: {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                }
            }
        },
        "methods": [
            {
                "name": "ping",
                "params": [],
                "result": {
                    "name": "result",
                    "schema": {"$ref": f"#/components/schemas/{name}"},
                },
            }
        ],
    }
    with pytest.raises(UnsupportedSchemaError):
        if language == "python":
            render_python_client(
                document, PythonClientOptions(package="collision_client")
            )
        else:
            render_typescript_client(document, TypeScriptClientOptions())
