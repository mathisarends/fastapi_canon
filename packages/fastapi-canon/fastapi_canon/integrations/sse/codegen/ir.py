from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi_canon.integrations.sse.codegen.names import identifier, slug
from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    Document,
    resolve_pointer,
    resolve_ref,
    validate_contract,
)

_REF_METADATA = {
    "title",
    "description",
    "default",
    "examples",
    "example",
    "deprecated",
    "readOnly",
    "writeOnly",
    "$comment",
    "externalDocs",
    "xml",
}

if TYPE_CHECKING:
    from fastapi_canon.integrations.sse.codegen.diagnostics import (
        GenerationDiagnostic,
        GenerationTarget,
    )


@dataclass(frozen=True)
class Scalar:
    name: Literal[
        "string", "integer", "number", "boolean", "null", "never", "JsonValue"
    ]
    constraints: tuple[tuple[str, str | int | float], ...] = ()


@dataclass(frozen=True)
class LiteralType:
    value: str | int | float | bool | None


@dataclass(frozen=True)
class EnumMember:
    name: str
    value: str


@dataclass(frozen=True)
class StringEnum:
    members: tuple[EnumMember, ...]


@dataclass(frozen=True)
class EnumAlias:
    target: str


@dataclass(frozen=True)
class EnumValue:
    enum: str
    member: str


@dataclass(frozen=True)
class Reference:
    name: str


@dataclass(frozen=True)
class Array:
    item: Type


@dataclass(frozen=True)
class Field:
    name: str
    type: Type
    required: bool
    default: LiteralType | None = None


@dataclass(frozen=True)
class Object:
    fields: tuple[Field, ...]
    additional: Type | None = None
    additional_forbidden: bool = False


@dataclass(frozen=True)
class Union:
    members: tuple[Type, ...]
    discriminator: str | None = None


@dataclass(frozen=True)
class Intersection:
    members: tuple[Type, ...]


type Type = (
    Scalar
    | LiteralType
    | Reference
    | Array
    | Object
    | Union
    | Intersection
    | StringEnum
    | EnumAlias
    | EnumValue
)


@dataclass(frozen=True)
class Declaration:
    name: str
    type: Type


@dataclass(frozen=True)
class Parameter:
    name: str
    argument: str
    location: Literal["path", "query", "header"]
    type: Type
    required: bool
    explode: bool = True


@dataclass(frozen=True)
class Body:
    type: Type
    required: bool
    media_type: str = "application/json"


@dataclass(frozen=True)
class ErrorResponse:
    status: str
    type: Type
    media_type: str


@dataclass(frozen=True)
class SecurityScheme:
    name: str
    kind: str
    location: str | None
    parameter: str | None


@dataclass(frozen=True)
class Operation:
    operation_id: str
    name: str
    group: str
    directory: str
    path: str
    method: str
    parameters: tuple[Parameter, ...]
    body: Body | None
    event: Type
    response_status: str
    errors: tuple[ErrorResponse, ...]
    security: tuple[dict[str, list[str]], ...]
    summary: str = ""
    description: str = ""
    deprecated: bool = False


@dataclass(frozen=True)
class Contract:
    operations: tuple[Operation, ...]
    declarations: tuple[Declaration, ...]
    security_schemes: tuple[SecurityScheme, ...]


def build_ir(
    document: Document,
    *,
    target: GenerationTarget = "python",
    diagnostics: list[GenerationDiagnostic] | None = None,
) -> Contract:
    validate_contract(document)
    types = _Types(document)
    operations: list[Operation] = []
    ids: set[str] = set()
    public_names: set[str] = set()
    names: set[tuple[str, str]] = set()
    groups: dict[str, str] = {}
    directories: dict[str, str] = {}
    paths: Document = document.get("paths", {})
    for path, raw_path in sorted(paths.items()):
        path_item = resolve_ref(document, raw_path)
        for method in (
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "options",
            "head",
            "trace",
        ):
            if method not in path_item:
                continue
            raw: Document = path_item[method]
            responses: Document = raw.get("responses", {})
            streams: list[tuple[str, Document]] = []
            errors: list[ErrorResponse] = []
            for status, value in sorted(responses.items()):
                response = resolve_ref(document, value)
                content: Document = response.get("content", {})
                if status.startswith("2") and "text/event-stream" in content:
                    streams.append((status, content["text/event-stream"]))
            if not streams:
                continue
            if len(streams) != 1 or method not in {"get", "post"}:
                raise ContractError(
                    f"Expected one GET/POST SSE success response: {method} {path}"
                )
            status, media = streams[0]
            envelope = resolve_ref(document, media.get("itemSchema", {}))
            data = resolve_ref(document, envelope.get("properties", {}).get("data", {}))
            if (
                data.get("contentMediaType") != "application/json"
                or "contentSchema" not in data
                or data.get("type") != "string"
                or "data" not in envelope.get("required", [])
                or envelope.get("type") != "object"
            ):
                raise ContractError(
                    f"SSE data needs application/json contentSchema: {path}"
                )
            operation_id: str = raw.get("operationId", "")
            if not operation_id or operation_id in ids:
                raise ContractError(
                    f"Missing or duplicate streaming operationId: {operation_id!r}"
                )
            ids.add(operation_id)
            public_name = identifier(operation_id, upper=True)
            if public_name in public_names:
                raise ContractError(f"Operation identifier collision: {public_name}")
            public_names.add(public_name)
            tag: str = (raw.get("tags") or ["streams"])[0]
            group, directory = identifier(tag), slug(tag)
            if (group in groups and groups[group] != tag) or (
                directory in directories and directories[directory] != tag
            ):
                raise ContractError(f"Tag name collision: {tag}")
            if group in {"transport", "types", "client", "index"}:
                raise ContractError(f"Reserved stream tag: {tag}")
            groups[group], directories[directory] = tag, tag
            name = identifier(operation_id)
            if (group, name) in names:
                raise ContractError(f"Operation name collision: {group}.{name}")
            names.add((group, name))
            merged: dict[tuple[str, str], Document] = {}
            for value in [*path_item.get("parameters", []), *raw.get("parameters", [])]:
                param = resolve_ref(document, value)
                merged[(param["in"], param["name"])] = param
            params: list[Parameter] = []
            arguments: set[str] = set()
            for (location, param_name), param in sorted(merged.items()):
                if location not in {"path", "query", "header"}:
                    raise ContractError(f"Unsupported parameter location: {location}")
                if "content" in param:
                    raise ContractError("Parameters with content are not supported")
                style = "form" if location == "query" else "simple"
                if param.get("style", style) != style or param.get(
                    "allowReserved", False
                ):
                    raise ContractError(
                        f"Unsupported serialization for parameter {param_name}"
                    )
                param_type = types.parse(param.get("schema", {}))
                if not _parameter_type(param_type, types, arrays=location == "query"):
                    supported = "scalar/array" if location == "query" else "scalar"
                    raise ContractError(
                        f"Only {supported} {location} parameters are supported: "
                        f"{param_name}"
                    )
                argument = identifier(param_name)
                if argument in arguments:
                    raise ContractError(f"Parameter name collision: {argument}")
                arguments.add(argument)
                params.append(
                    Parameter(
                        param_name,
                        argument,
                        cast(Any, location),
                        param_type,
                        param.get("required", False),
                        param.get("explode", location == "query"),
                    )
                )
            expected = set(re.findall(r"\{([^{}]+)\}", path))
            actual = {param.name for param in params if param.location == "path"}
            if expected != actual:
                raise ContractError(
                    f"Path placeholders do not match parameters: {path}"
                )
            body: Body | None = None
            if "requestBody" in raw:
                request = resolve_ref(document, raw["requestBody"])
                content = request.get("content", {})
                if method != "post" or set(content) != {"application/json"}:
                    raise ContractError(
                        "Streaming request bodies must be POST application/json"
                    )
                if "body" in arguments:
                    raise ContractError(
                        "Parameter 'body' conflicts with the request body"
                    )
                body = Body(
                    types.parse(content["application/json"].get("schema", {})),
                    request.get("required", False),
                )
            for code, value in sorted(responses.items()):
                if code.startswith("2"):
                    continue
                content = resolve_ref(document, value).get("content", {})
                for media_type, error_media in sorted(content.items()):
                    if "schema" in error_media:
                        errors.append(
                            ErrorResponse(
                                code, types.parse(error_media["schema"]), media_type
                            )
                        )
            operations.append(
                Operation(
                    operation_id,
                    name,
                    group,
                    directory,
                    path,
                    method.upper(),
                    tuple(params),
                    body,
                    types.parse(data["contentSchema"]),
                    status,
                    tuple(errors),
                    tuple(raw.get("security", document.get("security", []))),
                    raw.get("summary", ""),
                    raw.get("description", ""),
                    raw.get("deprecated", False),
                )
            )
    security: Document = document.get("components", {}).get("securitySchemes", {})
    schemes = tuple(
        SecurityScheme(name, scheme["type"], scheme.get("in"), scheme.get("name"))
        for name, value in sorted(security.items())
        for scheme in [resolve_ref(document, value)]
    )
    from fastapi_canon.integrations.sse.codegen.diagnostics import (
        collect_diagnostics,
        emit_diagnostics,
    )
    from fastapi_canon.integrations.sse.codegen.enums import with_enums

    contract = with_enums(Contract(tuple(operations), types.declarations(), schemes))
    found = collect_diagnostics(document, contract, target)
    if diagnostics is None:
        emit_diagnostics(found)
    else:
        diagnostics.extend(found)
    return contract


class _Types:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.names: dict[str, str] = {}
        self.pending: dict[str, Document | bool] = {}

    def parse(self, schema: Any) -> Type:
        if schema is True or schema == {}:
            return Scalar("JsonValue")
        if schema is False:
            return Scalar("never")
        if not isinstance(schema, dict):
            raise ContractError(f"Expected a Schema Object, got {schema!r}")
        node = cast(Document, schema)
        if "$ref" in node:
            ref: str = node["$ref"]
            if not ref.startswith("#/components/schemas/"):
                raise ContractError(
                    f"Type references must point to components/schemas: {ref}"
                )
            constraints = sorted(
                key
                for key in node
                if key != "$ref"
                and key not in _REF_METADATA
                and not key.startswith("x-")
            )
            if constraints:
                raise ContractError(
                    f"Unsupported constraints alongside $ref {ref}: {constraints}. "
                    "Define a standalone schema containing the full restrictions."
                )
            # Keep references in the IR so recursive models stay recursive.
            target = resolve_pointer(self.document, ref)
            if isinstance(target, dict):
                # Validate reference-only cycles without flattening the chain:
                # flattening would hide constraints on intermediate references.
                resolve_ref(self.document, cast(Document, target))
            name = identifier(
                ref.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~"), upper=True
            )
            if name == "JsonValue" or (name in self.names and self.names[name] != ref):
                raise ContractError(f"TypeScript type name collision: {name}")
            self.names[name] = ref
            self.pending[name] = target
            return Reference(name)
        constraints = {
            "minLength": "min_length",
            "maxLength": "max_length",
            "pattern": "pattern",
            "minimum": "ge",
            "maximum": "le",
            "exclusiveMinimum": "gt",
            "exclusiveMaximum": "lt",
            "multipleOf": "multiple_of",
        }
        if set(node) & constraints.keys() and any(
            key in node for key in ("const", "enum", "oneOf", "anyOf", "allOf")
        ):
            raise ContractError(
                "Scalar constraints alongside enum/const or composition are not "
                "supported; put constraints on scalar branches instead"
            )
        if "const" in node:
            return self.literal(node["const"])
        if "enum" in node:
            return Union(tuple(self.literal(value) for value in node["enum"]))
        for keyword in ("oneOf", "anyOf", "allOf"):
            if keyword in node:
                members = tuple(self.parse(part) for part in node[keyword])
                if keyword == "allOf":
                    combined: Type = Intersection(members)
                else:
                    combined = Union(
                        members, node.get("discriminator", {}).get("propertyName")
                    )
                siblings = {key: value for key, value in node.items() if key != keyword}
                if any(
                    key in siblings
                    for key in (
                        "type",
                        "properties",
                        "additionalProperties",
                        "oneOf",
                        "anyOf",
                        "allOf",
                    )
                ):
                    return Intersection((combined, self.parse(siblings)))
                return combined
        kind = node.get("type")
        if isinstance(kind, list):
            return Union(
                tuple(
                    self.parse({**node, "type": part}) for part in cast(list[str], kind)
                )
            )
        if kind in ("string", "boolean", "null", "number", "integer"):
            applicable: set[str] = (
                {"minLength", "maxLength", "pattern"}
                if kind == "string"
                else {
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                }
                if kind in ("number", "integer")
                else set()
            )
            return Scalar(
                kind,
                tuple(
                    (constraints[key], node[key])
                    for key in sorted(applicable & node.keys())
                ),
            )
        if kind == "array":
            if "prefixItems" in node:
                raise ContractError("Tuple schemas (prefixItems) are not supported")
            return Array(self.parse(node.get("items", {})))
        if kind == "object" or "properties" in node or "additionalProperties" in node:
            properties: Document = dict(node.get("properties", {}))
            required: list[str] = node.get("required", [])
            extra = node.get("additionalProperties", not properties)
            undeclared = set(required) - properties.keys()
            if undeclared and node.get("additionalProperties") is False:
                raise ContractError(
                    f"Required keys {sorted(undeclared)} are forbidden by "
                    "additionalProperties: false; declare them in properties."
                )
            # Required keys outside properties are governed by additionalProperties.
            # Omission of that keyword leaves their values unrestricted, even when
            # ordinary declared models retain the generator's default extra policy.
            properties.update(
                (name, node.get("additionalProperties", True))
                for name in sorted(undeclared)
            )
            return Object(
                tuple(
                    Field(
                        name,
                        self.parse(value),
                        name in required,
                        self.literal(value["default"])
                        if isinstance(value, dict)
                        and "default" in value
                        and isinstance(
                            value["default"], str | int | float | bool | None
                        )
                        else None,
                    )
                    for name, value in sorted(properties.items())
                ),
                self.parse(extra) if extra is not False else None,
                node.get("additionalProperties") is False,
            )
        if set(node) <= {
            "title",
            "description",
            "default",
            "examples",
            "deprecated",
            "readOnly",
            "writeOnly",
        }:
            return Scalar("JsonValue")
        raise ContractError(f"Unsupported or ambiguous JSON Schema: {node!r}")

    @staticmethod
    def literal(value: Any) -> LiteralType:
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ContractError("Object and array enum/const values are not supported")
        return LiteralType(value)

    def declarations(self) -> tuple[Declaration, ...]:
        result: dict[str, Declaration] = {}
        while todo := sorted(set(self.pending) - set(result)):
            for name in todo:
                result[name] = Declaration(name, self.parse(self.pending[name]))
        return tuple(result[name] for name in sorted(result))


def _parameter_type(
    type_: Type,
    types: _Types,
    seen: frozenset[str] = frozenset(),
    *,
    arrays: bool = True,
) -> bool:
    match type_:
        case Scalar(name=name):
            return name not in {"JsonValue", "never"}
        case LiteralType():
            return True
        case Array(item=item):
            return arrays and _parameter_type(item, types, seen, arrays=False)
        case Union(members=members):
            return all(
                _parameter_type(member, types, seen, arrays=arrays)
                for member in members
            )
        case Reference(name=name) if name not in seen:
            return _parameter_type(
                types.parse(types.pending[name]), types, seen | {name}, arrays=arrays
            )
        case _:
            return False
