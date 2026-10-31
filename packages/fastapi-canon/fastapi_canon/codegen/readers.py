from __future__ import annotations

from typing import Any

from fastapi_canon.codegen.config import SourceConfig
from fastapi_canon.codegen.ir import (
    Binding,
    GenerationError,
    Interaction,
    Operation,
    Parameter,
    Protocol,
    Source,
)
from fastapi_canon.codegen.names import camel, public_name, snake


def read_source(config: SourceConfig, document: dict[str, Any]) -> Source:
    try:
        if config.kind == "openapi":
            operations = _openapi(config, document)
        elif config.kind == "openrpc":
            operations = _openrpc(config, document)
        else:
            operations = _webrtc(config, document)
    except (KeyError, TypeError, AttributeError) as exc:
        raise GenerationError(
            f"{config.name}: invalid {config.kind} contract: {exc}"
        ) from exc
    known = {operation.binding.operation for operation in operations}
    missing = config.operations.keys() - known
    if missing:
        raise GenerationError(
            f"Source {config.name}: operation mappings do not exist: {sorted(missing)}"
        )
    return Source(config.name, config.kind, document, tuple(operations))


def _openapi(config: SourceConfig, document: dict[str, Any]) -> list[Operation]:
    from fastapi_canon.integrations.sse.codegen.names import (
        identifier,
        python_identifier,
    )
    from fastapi_canon.integrations.sse.codegen.openapi import resolve_ref

    version = document.get("openapi", "")
    if not isinstance(version, str) or not version.startswith(("3.1.", "3.2.")):
        raise GenerationError(f"{config.name}: OpenAPI 3.1 or 3.2 is required")
    operations = []
    interaction: Interaction
    ids: set[str] = set()
    for path, raw_path in sorted(document.get("paths", {}).items()):
        path_item = resolve_ref(document, raw_path)
        for method in (
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "trace",
        ):
            if method not in path_item:
                continue
            raw = path_item[method]
            identity = raw.get("operationId")
            if not isinstance(identity, str) or not identity or identity in ids:
                raise GenerationError(
                    f"{config.name}: operations require unique operationId values ({method.upper()} {path})"
                )
            ids.add(identity)
            default = (
                identity
                if "." in identity
                else ".".join([*(raw.get("tags") or ["api"])[:1], identity])
            )
            name = public_name(
                config.operations.get(identity, raw.get("x-canon-operation", default))
            )
            parameters = []
            seen: dict[tuple[str, str], Any] = {}
            for parameter in [
                *path_item.get("parameters", []),
                *raw.get("parameters", []),
            ]:
                value = resolve_ref(document, parameter)
                seen[value["in"], value["name"]] = value
            for (location, wire_name), value in seen.items():
                if location not in {"path", "query", "header"} or "schema" not in value:
                    raise GenerationError(
                        f"{config.name}:{identity}: unsupported parameter {wire_name}"
                    )
                schema = value["schema"]
                if not _http_parameter(schema, document, array=location == "query"):
                    raise GenerationError(
                        f"{config.name}:{identity}: unsupported {location} parameter schema for {wire_name}"
                    )
                style = value.get(
                    "style", "form" if location in {"query", "cookie"} else "simple"
                )
                if (
                    style != ("form" if location == "query" else "simple")
                    or value.get("allowReserved")
                    or (
                        not value.get("explode", location == "query")
                        and location == "query"
                    )
                ):
                    raise GenerationError(
                        f"{config.name}:{identity}: unsupported parameter serialization for {wire_name}"
                    )
                parameters.append(
                    Parameter(
                        wire_name,
                        wire_name,
                        schema,
                        value.get("required", False),
                        location,
                    )
                )
            body = raw.get("requestBody")
            if body is not None:
                body = resolve_ref(document, body)
                media = body.get("content", {})
                if set(media) != {"application/json"}:
                    raise GenerationError(
                        f"{config.name}:{identity}: only JSON request bodies are supported"
                    )
                parameters.append(
                    Parameter(
                        "body",
                        "body",
                        media["application/json"].get("schema", {}),
                        body.get("required", False),
                        "body",
                    )
                )
            successes = {}
            bodyless = []
            streams = []
            for status, raw_response in raw.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                response = resolve_ref(document, raw_response)
                content = response.get("content", {})
                if "text/event-stream" in content:
                    streams.append((status, content["text/event-stream"]))
                elif not content:
                    successes[status] = {"type": "null"}
                    bodyless.append(status)
                elif set(content) == {"application/json"}:
                    successes[status] = content["application/json"].get("schema", {})
                else:
                    raise GenerationError(
                        f"{config.name}:{identity}: unsupported success response media type"
                    )
            if streams:
                if len(streams) != 1 or successes:
                    raise GenerationError(
                        f"{config.name}:{identity}: ambiguous streaming response"
                    )
                profile = raw.get("x-canon-stream")
                envelope = (
                    profile.get("businessItemSchema", {})
                    if profile
                    else streams[0][1].get("itemSchema", {})
                )
                data = envelope.get("properties", {}).get("data", {})
                if (
                    data.get("contentMediaType") != "application/json"
                    or "contentSchema" not in data
                ):
                    raise GenerationError(
                        f"{config.name}:{identity}: streams require a typed JSON-data SSE contract"
                    )
                result = data["contentSchema"]
                binding = Binding(
                    config.name,
                    "sse",
                    "http",
                    identity,
                    (
                        identifier((raw.get("tags") or ["streams"])[0]),
                        python_identifier(identity),
                    ),
                    path,
                    method.upper(),
                    {
                        "backend_python": (
                            python_identifier(
                                identifier((raw.get("tags") or ["streams"])[0])
                            ),
                            python_identifier(identity),
                        ),
                        "backend_typescript": (
                            identifier((raw.get("tags") or ["streams"])[0]),
                            identifier(identity),
                        ),
                        "python_arguments": {
                            param.name: python_identifier(param.wire_name)
                            for param in parameters
                        },
                        "typescript_arguments": {
                            param.name: identifier(param.wire_name)
                            for param in parameters
                        },
                        "profile": bool(profile),
                    },
                )
                interaction = "stream"
            else:
                if not successes:
                    raise GenerationError(
                        f"{config.name}:{identity}: no documented successful response"
                    )
                result = _union(list(successes.values()))
                binding = Binding(
                    config.name,
                    "http",
                    "http",
                    identity,
                    path=path,
                    method=method.upper(),
                    metadata={"responses": successes, "bodyless": bodyless},
                )
                interaction = "call"
            _parameter_names(parameters, config.name, identity)
            operations.append(
                Operation(
                    name,
                    interaction,
                    binding,
                    tuple(parameters),
                    result,
                    raw.get("summary", ""),
                )
            )
    return operations


def _openrpc(config: SourceConfig, document: dict[str, Any]) -> list[Operation]:
    from fastapi_canon.integrations.websockets.codegen.ir import build_ir
    from fastapi_canon.integrations.websockets.codegen.names import camel_case
    from fastapi_canon.integrations.websockets.codegen.python import (
        _identifier as python_identifier,
    )

    ir = build_ir(document)
    if any(
        server.transport is not None and server.transport.type != "websocket"
        for server in ir.servers
    ):
        raise GenerationError(
            f"{config.name}: this implementation binds JSON-RPC to WebSocket"
        )
    schemas = document.get("components", {}).get("schemas", {})
    operations = []
    routes: tuple[Any, ...] = (
        *ir.operations,
        *ir.notifications,
        *ir.binary_streams,
        *ir.sockets,
    )
    for route in routes:
        kind: Interaction = "call"
        protocol: Protocol = "jsonrpc"
        parameters = []
        result: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        name = route.rpc_name
        if route in ir.operations:
            raw = next(item for item in document["methods"] if item["name"] == name)
            for param in raw.get("params", []):
                parameters.append(
                    Parameter(
                        param["name"],
                        param["name"],
                        param.get("schema", {}),
                        param.get("required", False),
                    )
                )
            result = raw.get("result", {}).get("schema", {"type": "null"})
        elif route in ir.notifications:
            kind = "stream"
            subscription = getattr(route, "subscription", False)
            key = "x-rpc-subscriptions" if subscription else "x-rpc-notifications"
            raw = next(item for item in document.get(key, []) if item["name"] == name)
            result = raw["payload"]
            metadata["subscription"] = subscription
            params = schemas.get(getattr(route, "params_model", None), {})
            for param, schema in params.get("properties", {}).items():
                parameters.append(
                    Parameter(param, param, schema, param in params.get("required", []))
                )
        else:
            kind = "session"
            protocol = "websocket"
            metadata["binary"] = route in ir.binary_streams
            metadata["backend_type"] = "binary" if metadata["binary"] else "socket"
            if metadata["binary"]:
                metadata["python_session"] = {
                    "client-to-server": "BinarySender",
                    "bidirectional": "BinaryChannel",
                    "server-to-client": "BinaryReceiver",
                }[route.direction]
            else:
                raw_socket = next(
                    item
                    for item in document.get("x-rpckit-sockets", [])
                    if item["name"] == name
                )
                metadata["messages"] = raw_socket.get("messages", {})

            for variable in route.call_variables:
                parameters.append(
                    Parameter(variable.name, variable.name, {"type": "string"}, False)
                )
        _parameter_names(parameters, config.name, name)
        public = public_name(config.operations.get(name, name))
        metadata["backend_method"] = route.operation_name
        metadata["python_arguments"] = {
            param.name: python_identifier(param.wire_name) for param in parameters
        }
        metadata["backend_python"] = (
            *[python_identifier(part) for part in route.path],
            python_identifier(route.operation_name),
        )
        metadata["backend_typescript"] = (
            *[camel_case(part) for part in route.path],
            camel_case(route.operation_name),
        )
        operations.append(
            Operation(
                public,
                kind,
                Binding(
                    config.name,
                    protocol,
                    "websocket",
                    name,
                    (*route.path, route.operation_name),
                    metadata=metadata,
                ),
                tuple(parameters),
                result,
                route.summary,
            )
        )
    return operations


def _webrtc(config: SourceConfig, document: dict[str, Any]) -> list[Operation]:
    from fastapi_canon.integrations.webrtc.codegen.typescript import (
        generate_typescript as generate,
    )

    generate(document)
    operations = []
    for session in document["sessions"]:
        identity = session["name"]
        name = public_name(
            config.operations.get(identity, session.get("x-canon-operation", identity))
        )
        # A session remains an explicit object with its own channel/media contract.
        operations.append(
            Operation(
                name,
                "session",
                Binding(
                    config.name,
                    "webrtc",
                    "webrtc",
                    identity,
                    (camel(identity), "connect"),
                ),
                summary=session.get("description", ""),
            )
        )
    return operations


def _parameter_names(parameters: list[Parameter], source: str, operation: str) -> None:
    for convert in (camel, snake):
        names = [convert(parameter.name) for parameter in parameters]
        if len(set(names)) != len(names) or any(
            name in {"options", "self"} for name in names
        ):
            raise GenerationError(
                f"{source}:{operation}: ambiguous or reserved public parameter names"
            )


def _union(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    distinct = []
    for schema in schemas:
        if schema not in distinct:
            distinct.append(schema)
    return distinct[0] if len(distinct) == 1 else {"anyOf": distinct}


def _http_parameter(schema: Any, document: dict[str, Any], *, array: bool) -> bool:
    from fastapi_canon.integrations.sse.codegen.openapi import resolve_ref

    schema = resolve_ref(document, schema)
    if not isinstance(schema, dict):
        return False
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return all(
                _http_parameter(branch, document, array=array)
                for branch in schema[union]
            )
    kind = schema.get("type")
    if isinstance(kind, list):
        return all(
            _http_parameter({**schema, "type": value}, document, array=array)
            for value in kind
        )
    if kind in {"string", "integer", "number", "boolean", "null"}:
        return True
    if kind == "array" and array:
        return _http_parameter(schema.get("items", {}), document, array=False)
    return "enum" in schema and all(
        value is None or isinstance(value, (str, int, float, bool))
        for value in schema["enum"]
    )
