"""Generate a typed TypeScript browser client from a session contract.

The output consists of four modules:

- ``runtime.ts``: the static, framework-free signaling and session runtime.
- ``models.ts``: TypeScript types for the shared JSON Schema definitions.
- ``client.ts``: per-session channel, parameter and option types, embedded
  session descriptors and the client class.
- ``index.ts``: re-exports of the three modules above.
"""

import json
import re
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

from fastapi_canon.integrations.webrtc.protocol import PROTOCOL_VERSION, SUBPROTOCOL

CONTRACT_VERSION = 1
DEFAULT_CLIENT_NAME = "RtcClient"

_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_RESERVED_WORDS = frozenset(
    [
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "null",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "as",
        "implements",
        "interface",
        "let",
        "package",
        "private",
        "protected",
        "public",
        "static",
        "yield",
        "any",
        "boolean",
        "number",
        "string",
        "symbol",
        "type",
        "unknown",
        "never",
        "object",
        "bigint",
        "undefined",
        "await",
        "async",
        "constructor",
    ]
)
# Option names consumed by the runtime; parameters cannot reuse them.
RESERVED_OPTIONS = frozenset(
    {
        "tracks",
        "signal",
        "timeoutMs",
        "query",
        "rtcConfiguration",
        "sendBufferHighBytes",
        "sendTimeoutMs",
        "onTrack",
        "onMessage",
        "onStateChange",
        "onError",
        "onClose",
    }
)
_EXPORTED = re.compile(
    r"^export (?:abstract )?(?:class|function|async function|const|type|interface)"
    r" (\w+)",
    re.MULTILINE,
)


class ContractError(ValueError):
    """The contract cannot be turned into a client."""


def runtime_source() -> str:
    """Return the static TypeScript runtime shipped with the package."""
    return (
        resources.files("fastapi_canon.integrations.webrtc.codegen")
        .joinpath("templates/runtime.ts")
        .read_text(encoding="utf-8")
    )


def generate_typescript(
    contract: Mapping[str, Any],
    *,
    client_name: str = DEFAULT_CLIENT_NAME,
    header: str = "",
) -> dict[str, str]:
    """Return the generated files, keyed by their relative path."""
    _check_contract(contract)
    if not _IDENTIFIER.fullmatch(client_name) or client_name in _RESERVED_WORDS:
        raise ContractError(f"--client-name {client_name!r} is not a TS identifier")
    runtime = runtime_source()
    sessions: list[Mapping[str, Any]] = list(contract["sessions"])
    names_in_use = {name: "the runtime" for name in _EXPORTED.findall(runtime)}
    names_in_use["models"] = "the models import"

    def claim(registry: dict[str, str], name: str, owner: str) -> None:
        previous = registry.get(name)
        if previous is not None:
            raise ContractError(
                f"Generated name {name!r} for {owner} clashes with {previous}; "
                "rename the session or pass another --client-name"
            )
        registry[name] = owner

    claim(names_in_use, client_name, "the client class")
    members = {"config": "the client configuration member"}
    for session in sessions:
        pascal = _pascal(session["name"])
        owner = f"session {session['name']!r}"
        claim(members, _camel(session["name"]), owner)
        for name in (
            f"{pascal}Channels",
            f"{pascal}Parameters",
            f"{pascal}ConnectOptions",
            f"{pascal}Session",
            f"{_camel(session['name'])}Descriptor",
        ):
            claim(names_in_use, name, owner)
    names = _definition_names(contract.get("definitions", {}), set(names_in_use))
    emitter = _Emitter(names)
    banner = _banner(header)
    return {
        "runtime.ts": runtime,
        "models.ts": banner + _models(contract.get("definitions", {}), emitter),
        "client.ts": banner + _client(sessions, client_name, emitter),
        "index.ts": banner
        + 'export * from "./runtime.js";\n'
        + 'export * from "./models.js";\n'
        + 'export * from "./client.js";\n',
    }


def _check_contract(contract: Mapping[str, Any]) -> None:
    if not isinstance(contract, Mapping):
        raise ContractError("The contract must be a JSON object")
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(
            f"Unsupported contract version {contract.get('contract_version')!r}; "
            f"this generator reads version {CONTRACT_VERSION}"
        )
    if (
        contract.get("protocol") != PROTOCOL_VERSION
        or contract.get("subprotocol") != SUBPROTOCOL
    ):
        raise ContractError(
            f"The contract uses protocol {contract.get('subprotocol')!r}; the "
            f"runtime speaks {SUBPROTOCOL!r}"
        )
    if not isinstance(contract.get("sessions"), list):
        raise ContractError("The contract has no session list")


def _banner(header: str) -> str:
    lines = ["// Generated by fastapi-canon; do not edit. Regenerate the client."]
    if header:
        lines.append(f"// {header}")
    return "\n".join(lines) + "\n\n"


# ------------------------------------------------------------------ models


def _definition_names(
    definitions: Mapping[str, Any], taken: set[str]
) -> dict[str, str]:
    names: dict[str, str] = {}
    used = set(taken)
    for name in sorted(definitions):
        base = re.sub(r"[^A-Za-z0-9_$]", "_", name).strip("_") or "Model"
        if base[0].isdigit():
            base = f"_{base}"
        candidate = base
        if candidate in used or candidate in _RESERVED_WORDS:
            candidate = f"{base}Model"
        index = 2
        while candidate in used or candidate in _RESERVED_WORDS:
            candidate = f"{base}Model{index}"
            index += 1
        used.add(candidate)
        names[name] = candidate
    return names


def _models(definitions: Mapping[str, Any], emitter: "_Emitter") -> str:
    if not definitions:
        return "export {};\n"
    blocks: list[str] = []
    for name in sorted(definitions):
        schema = definitions[name]
        identifier = emitter.names[name]
        doc = _doc(_schema_doc(schema), "")
        if _is_plain_object(schema):
            body = emitter.object_body(schema, "")
            blocks.append(f"{doc}export interface {identifier} {body}\n")
        else:
            blocks.append(f"{doc}export type {identifier} = {emitter.type(schema)};\n")
    return "\n".join(blocks)


def _is_plain_object(schema: Any) -> bool:
    return (
        isinstance(schema, Mapping)
        and schema.get("type") == "object"
        and "properties" in schema
        and not any(key in schema for key in ("anyOf", "oneOf", "allOf", "$ref"))
    )


class _Emitter:
    """Translate the JSON Schema subset produced by Pydantic into TypeScript."""

    def __init__(self, names: Mapping[str, str], prefix: str = "") -> None:
        self.names = names
        self.prefix = prefix

    def scoped(self, prefix: str) -> "_Emitter":
        return _Emitter(self.names, prefix)

    def type(self, schema: Any, indent: str = "") -> str:
        if schema is True or schema is None:
            return "unknown"
        if schema is False:
            return "never"
        if not isinstance(schema, Mapping):
            raise ContractError(f"Invalid JSON Schema: {schema!r}")
        if "$ref" in schema:
            reference = schema["$ref"]
            prefix = "#/definitions/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ContractError(f"Unsupported schema reference {reference!r}")
            target = reference.removeprefix(prefix)
            if target not in self.names:
                raise ContractError(f"Unknown schema reference {reference!r}")
            return self.prefix + self.names[target]
        if "const" in schema:
            return _literal(schema["const"])
        if "enum" in schema:
            values = schema["enum"]
            if not isinstance(values, list) or not values:
                return "never"
            return _union([_literal(value) for value in values])
        for key in ("anyOf", "oneOf"):
            if key in schema:
                return _union([self.type(item, indent) for item in schema[key]])
        if "allOf" in schema:
            parts = [self.type(item, indent) for item in schema["allOf"]]
            return " & ".join(_wrap(part) for part in parts) if parts else "unknown"
        kind = schema.get("type")
        if isinstance(kind, list):
            return _union(
                [self.type({**schema, "type": item}, indent) for item in kind]
            )
        if kind == "string":
            return "string"
        if kind in ("integer", "number"):
            return "number"
        if kind == "boolean":
            return "boolean"
        if kind == "null":
            return "null"
        if kind == "array":
            return self._array(schema, indent)
        if kind == "object" or "properties" in schema:
            return self.object_body(schema, indent)
        return "unknown"

    def _array(self, schema: Mapping[str, Any], indent: str) -> str:
        prefix_items = schema.get("prefixItems")
        items = schema.get("items", True)
        if isinstance(prefix_items, list):
            elements = [self.type(item, indent) for item in prefix_items]
            minimum = schema.get("minItems", len(elements))
            rendered = [
                element if index < minimum else f"{_wrap(element)}?"
                for index, element in enumerate(elements)
            ]
            if items is not False and schema.get("maxItems") != len(elements):
                rendered.append(f"...{_wrap(self.type(items, indent))}[]")
            return f"[{', '.join(rendered)}]"
        return f"{_wrap(self.type(items, indent))}[]"

    def object_body(self, schema: Mapping[str, Any], indent: str) -> str:
        properties: Mapping[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", ()))
        additional = schema.get("additionalProperties", True)
        if not properties:
            if additional is False:
                return "Record<string, never>"
            value = "unknown" if additional is True else self.type(additional, indent)
            return f"Record<string, {value}>"
        inner = indent + "  "
        lines = ["{"]
        for name, item in properties.items():
            optional = "" if name in required else "?"
            lines.append(_doc(_schema_doc(item), inner).rstrip("\n"))
            lines.append(
                f"{inner}{_property(name)}{optional}: {self.type(item, inner)};"
            )
        if additional is not True and additional is not False:
            lines.append(f"{inner}[key: string]: unknown;")
        lines.append(f"{indent}}}")
        return "\n".join(line for line in lines if line)


def _schema_doc(schema: Any) -> str | None:
    if not isinstance(schema, Mapping):
        return None
    description = schema.get("description")
    return description if isinstance(description, str) else None


def _doc(text: str | None, indent: str) -> str:
    if not text:
        return ""
    lines = text.replace("*/", "*\\/").strip().splitlines()
    if len(lines) == 1:
        return f"{indent}/** {lines[0]} */\n"
    body = "\n".join(f"{indent} *{' ' + line if line else ''}" for line in lines)
    return f"{indent}/**\n{body}\n{indent} */\n"


def _literal(value: Any) -> str:
    if value is None or isinstance(value, bool | int | float | str):
        return json.dumps(value)
    raise ContractError(f"Unsupported literal {value!r}")


def _union(parts: Sequence[str]) -> str:
    unique = list(dict.fromkeys(parts))
    if "unknown" in unique:
        return "unknown"
    return " | ".join(unique) if unique else "never"


def _wrap(part: str) -> str:
    if " | " in part or " & " in part or part.startswith("..."):
        return f"({part})"
    return part


def _property(name: str) -> str:
    return name if _IDENTIFIER.fullmatch(name) else json.dumps(name)


# ------------------------------------------------------------------ client

_RECEIVE_TYPES = {
    "raw": "string | ArrayBuffer",
    "text": "string",
    "binary": "ArrayBuffer",
    "none": "never",
}
_SEND_TYPES = {
    "raw": "string | ArrayBuffer | ArrayBufferView",
    "text": "string",
    "binary": "ArrayBuffer | ArrayBufferView",
    "none": "never",
}


def _client(
    sessions: Sequence[Mapping[str, Any]], client_name: str, emitter: _Emitter
) -> str:
    models = emitter.scoped("models.")
    runtime_types = [
        "ClientConfig",
        "ConnectOptions",
        "RtcSession",
        "SessionApi",
        "SessionDescriptor",
    ]
    out = [
        'import type * as models from "./models.js";\n' if emitter.names else "",
        "import {\n"
        + "".join(f"  type {name},\n" for name in runtime_types)
        + "  sessionApi,\n"
        + '} from "./runtime.js";\n',
    ]
    for session in sessions:
        out.append("\n" + _session_types(session, models))
    out.append(_client_class(sessions, client_name))
    return "".join(out)


def _session_types(session: Mapping[str, Any], models: _Emitter) -> str:
    name: str = session["name"]
    pascal = _pascal(name)
    camel = _camel(name)
    parts: list[str] = []

    channels: Sequence[Mapping[str, Any]] = session["channels"]
    parts.append(
        f"/** Data channels of the `{name}` session, from the client's point of "
        "view. */\n"
    )
    if channels:
        lines = [f"export type {pascal}Channels = {{"]
        for channel in channels:
            send = _message_type(
                channel["receive_format"],
                channel.get("receive_schema"),
                _SEND_TYPES,
                models,
            )
            receive = _message_type(
                channel["send_format"],
                channel.get("send_schema"),
                _RECEIVE_TYPES,
                models,
            )
            lines.append(
                f"  readonly {_property(channel['name'])}: {{\n"
                f"    readonly send: {send};\n"
                f"    readonly receive: {receive};\n"
                "  };"
            )
        lines.append("};\n")
        parts.append("\n".join(lines))
    else:
        parts.append(f"export type {pascal}Channels = Record<never, never>;\n")

    parameters = _parameters(session)
    options = f"ConnectOptions<{pascal}Channels>"
    if parameters:
        lines = [
            f"\n/** Path and query parameters of the `{name}` session. */",
            f"export type {pascal}Parameters = {{",
        ]
        for key, parameter in parameters:
            # Query requirements apply after shared credentials and per-call
            # query values are merged. Path parameters always stay local.
            optional = (
                "" if parameter["required"] and parameter["in"] == "path" else "?"
            )
            lines.append(_doc(_schema_doc(parameter.get("schema")), "  ").rstrip("\n"))
            value = models.type(parameter.get("schema"), "  ")
            lines.append(f"  readonly {key}{optional}: {value};")
        lines.append("};\n")
        parts.append("\n".join(line for line in lines if line))
        options = f"{options} & {pascal}Parameters"
    parts.append(f"\nexport type {pascal}ConnectOptions = {options};\n")
    parts.append(f"export type {pascal}Session = RtcSession<{pascal}Channels>;\n")

    descriptor = {
        "name": name,
        "path": session["path"],
        "role": session["role"],
        "media": {
            "audio": session["media"]["audio"],
            "video": session["media"]["video"],
        },
        "channels": [
            {
                "name": channel["name"],
                "receiveFormat": channel["receive_format"],
                "sendFormat": channel["send_format"],
            }
            for channel in channels
        ],
        "parameters": [
            {
                "name": parameter["name"],
                "key": key,
                "in": parameter["in"],
                "required": parameter["required"],
            }
            for key, parameter in parameters
        ],
    }
    if "admission_errors" in session:
        descriptor["admissionErrors"] = session["admission_errors"]
    parts.append(
        f"\nexport const {camel}Descriptor: SessionDescriptor = "
        f"{_ts_value(descriptor, '')};\n"
    )
    return "".join(parts)


def _message_type(
    message_format: str,
    schema: Any,
    fallback: Mapping[str, str],
    models: _Emitter,
) -> str:
    if message_format == "json":
        return models.type(schema, "    ")
    try:
        return fallback[message_format]
    except KeyError:
        raise ContractError(f"Unknown message format {message_format!r}") from None


def _parameters(
    session: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    result: list[tuple[str, Mapping[str, Any]]] = []
    keys: dict[str, str] = {}
    for parameter in session.get("parameters", []):
        if parameter.get("in") not in ("path", "query"):
            raise ContractError(f"Unsupported parameter location {parameter!r}")
        key = _camel(parameter["name"])
        if key in RESERVED_OPTIONS:
            raise ContractError(
                f"Parameter {parameter['name']!r} of session {session['name']!r} "
                f"collides with the connect option {key!r}; give it an alias"
            )
        if key in keys:
            raise ContractError(
                f"Parameters {keys[key]!r} and {parameter['name']!r} of session "
                f"{session['name']!r} both map to {key!r}"
            )
        keys[key] = parameter["name"]
        result.append((key, parameter))
    return result


def _client_class(sessions: Sequence[Mapping[str, Any]], client_name: str) -> str:
    lines = [
        "",
        "/** Typed WebRTC sessions of the application. */",
        f"export class {client_name} {{",
    ]
    for session in sessions:
        pascal = _pascal(session["name"])
        doc = _doc(session.get("description"), "  ")
        if doc:
            lines.append(doc.rstrip("\n"))
        lines.append(
            f"  readonly {_camel(session['name'])}: "
            f"SessionApi<{pascal}Channels, {pascal}ConnectOptions>;"
        )
    if sessions:
        lines.append("")
    lines.append("  constructor(readonly config: ClientConfig = {}) {")
    for session in sessions:
        pascal = _pascal(session["name"])
        camel = _camel(session["name"])
        lines.append(
            f"    this.{camel} = sessionApi<{pascal}Channels, {pascal}ConnectOptions>"
            f"(config, {camel}Descriptor);"
        )
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _ts_value(value: Any, indent: str) -> str:
    inner = indent + "  "
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        items = [
            f"{inner}{_property(k)}: {_ts_value(v, inner)}," for k, v in value.items()
        ]
        return "{\n" + "\n".join(items) + f"\n{indent}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [f"{inner}{_ts_value(v, inner)}," for v in value]
        return "[\n" + "\n".join(items) + f"\n{indent}]"
    return json.dumps(value, ensure_ascii=False)


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced) if word]


def _pascal(name: str) -> str:
    result = "".join(word[:1].upper() + word[1:] for word in _words(name)) or "Session"
    return f"_{result}" if result[0].isdigit() else result


def _camel(name: str) -> str:
    pascal = _pascal(name).lstrip("_")
    result = pascal[:1].lower() + pascal[1:]
    if not result or result[0].isdigit() or result in _RESERVED_WORDS:
        return f"_{result}"
    return result
