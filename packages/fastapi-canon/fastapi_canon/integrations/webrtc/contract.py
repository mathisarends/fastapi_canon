"""Export declared sessions as a JSON contract for client generators.

The contract lists every ``WebRtcRouter`` session reachable from an application:
its effective path, role, media policy, data channels with JSON Schemas of their
messages, and the path and query parameters that its dependencies read. Header
and cookie parameters are omitted because browsers cannot set them on a
WebSocket; cookies are sent automatically.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from copy import copy, deepcopy
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode
from pydantic_core import core_schema
from starlette.routing import BaseRoute

from fastapi_canon.integrations.webrtc.declarations import (
    DataChannelSpec,
    SessionDeclaration,
)
from fastapi_canon.integrations.webrtc.protocol import PROTOCOL_VERSION, SUBPROTOCOL
from fastapi_canon.integrations.webrtc.router import DECLARATION_ATTRIBUTE

if TYPE_CHECKING:
    # Only the documented attributes path_params, query_params and dependencies
    # of a route's dependant are read; no private FastAPI module is imported.
    from fastapi.dependencies.models import Dependant

# Builds Annotated types from runtime values, which type checkers cannot follow.
_ANNOTATED: Any = Annotated

CONTRACT_VERSION = 1
REF_TEMPLATE = "#/definitions/{model}"

type ParameterLocation = Literal["path", "query"]


class _SchemaGenerator(GenerateJsonSchema):
    """Mark defaulted fields required in serialization schemas: the server always
    sends them, so generated receive types should not make them optional."""

    def field_is_required(
        self,
        field: core_schema.ModelField
        | core_schema.DataclassField
        | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        if self.mode == "serialization":
            return not field.get("serialization_exclude")
        return super().field_is_required(field, total)


def build_contract(app: FastAPI | Sequence[BaseRoute]) -> dict[str, Any]:
    """Build the contract of every session declared in ``app``."""
    routes = app.routes if isinstance(app, FastAPI) else app
    sessions: list[tuple[str, SessionDeclaration, Dependant | None]] = []
    seen: set[str] = set()
    for path, endpoint, dependant in _session_routes(routes):
        declaration: SessionDeclaration = getattr(endpoint, DECLARATION_ATTRIBUTE)
        if declaration.name in seen:
            raise ValueError(
                f"Session name {declaration.name!r} is declared more than once; "
                "generated clients need unique names"
            )
        seen.add(declaration.name)
        sessions.append((path, declaration, dependant))

    inputs: list[tuple[tuple[str, str], JsonSchemaMode, TypeAdapter[Any]]] = []
    for _, declaration, dependant in sessions:
        for spec in declaration.channels:
            if spec.receive_adapter is not None:
                inputs.append(
                    (
                        (declaration.name, f"{spec.name}:receive"),
                        "validation",
                        spec.receive_adapter,
                    )
                )
            if spec.send_adapter is not None:
                inputs.append(
                    (
                        (declaration.name, f"{spec.name}:send"),
                        "serialization",
                        spec.send_adapter,
                    )
                )
        for location, name, annotation, _required in _parameters(dependant):
            inputs.append(
                (
                    (declaration.name, f"{location}:{name}"),
                    "validation",
                    TypeAdapter(annotation),
                )
            )
    schemas: dict[tuple[str, str], Any] = {}
    definitions: dict[str, Any] = {}
    if inputs:
        mapping, defs = TypeAdapter.json_schemas(
            inputs, ref_template=REF_TEMPLATE, schema_generator=_SchemaGenerator
        )
        schemas = {key: schema for (key, _mode), schema in mapping.items()}
        definitions = dict(defs.get("$defs", {}))

    return {
        "contract_version": CONTRACT_VERSION,
        "protocol": PROTOCOL_VERSION,
        "subprotocol": SUBPROTOCOL,
        "sessions": [
            _session(
                path,
                declaration,
                dependant,
                schemas,
                app if isinstance(app, FastAPI) else None,
            )
            for path, declaration, dependant in sorted(
                sessions, key=lambda s: s[1].name
            )
        ],
        "definitions": dict(sorted(definitions.items())),
    }


def dumps(contract: dict[str, Any]) -> str:
    """Serialize a contract deterministically."""
    return json.dumps(contract, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _session(
    path: str,
    declaration: SessionDeclaration,
    dependant: Dependant | None,
    schemas: dict[tuple[str, str], Any],
    app: FastAPI | None,
) -> dict[str, Any]:
    media = declaration.media
    result = {
        "name": declaration.name,
        "path": path,
        "role": declaration.role.value,
        "description": declaration.description,
        "parameters": [
            {
                "name": name,
                "in": location,
                "required": required,
                "schema": schemas[(declaration.name, f"{location}:{name}")],
            }
            for location, name, _annotation, required in _parameters(dependant)
        ],
        "media": {
            "audio": None if media.audio is None else media.audio.value,
            "video": None if media.video is None else media.video.value,
            "max_incoming_tracks": media.max_incoming_tracks,
        },
        "channels": [
            _channel(declaration, spec, schemas) for spec in declaration.channels
        ],
    }
    if declaration.admission_errors is not None:
        result["admission_errors"] = [
            deepcopy(dict(error)) for error in declaration.admission_errors(app)
        ]
    return result


def _channel(
    declaration: SessionDeclaration,
    spec: DataChannelSpec,
    schemas: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "id": declaration.channel_ids.get(spec.name),
        "negotiated": spec.negotiated,
        "ordered": spec.ordered,
        "max_retransmits": spec.max_retransmits,
        "max_packet_life_time_ms": spec.max_packet_life_time_ms,
        "max_message_bytes": declaration.max_message_bytes(spec),
        "receive_format": spec.receive_format,
        "send_format": spec.send_format,
        "receive_schema": schemas.get((declaration.name, f"{spec.name}:receive")),
        "send_schema": schemas.get((declaration.name, f"{spec.name}:send")),
    }


def _session_routes(
    routes: Sequence[BaseRoute],
) -> Iterator[tuple[str, Any, Dependant | None]]:
    for path, route in _flatten(routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, DECLARATION_ATTRIBUTE):
            continue
        yield path, endpoint, getattr(route, "dependant", None)


def _flatten(routes: Sequence[BaseRoute]) -> Iterator[tuple[str, Any]]:
    try:
        from fastapi.routing import iter_route_contexts  # FastAPI >= 0.140
    except ImportError:  # pragma: no cover - depends on the FastAPI version
        for route in routes:
            path = getattr(route, "path", None)
            if path is not None:
                yield path, route
        return
    for context in iter_route_contexts(routes):
        if context.path is not None:
            yield context.path, context


def _parameters(
    dependant: Dependant | None,
) -> list[tuple[ParameterLocation, str, Any, bool]]:
    if dependant is None:
        return []
    found: dict[tuple[str, str], tuple[ParameterLocation, str, Any, bool]] = {}
    for location, alias, field_info in _fields(dependant):
        key = (location, alias)
        required = bool(field_info.is_required())
        previous = found.get(key)
        if previous is not None:
            required = required or previous[3]
        # Keep constraints and descriptions of Query()/Path() declarations.
        info = copy(field_info)
        info.alias = info.validation_alias = info.serialization_alias = None
        annotation = _ANNOTATED[info.annotation, info]
        found[key] = (location, alias, annotation, required)
    path_order = [v for (loc, _), v in found.items() if loc == "path"]
    query_order = [v for (loc, _), v in found.items() if loc == "query"]
    return path_order + query_order


def _fields(
    dependant: Dependant,
) -> Iterable[tuple[ParameterLocation, str, FieldInfo]]:
    """Yield ``(location, wire name, field info)`` of every path/query field."""
    for field in dependant.path_params:
        yield "path", _wire_name(field), field.field_info
    query = dependant.query_params
    annotation = query[0].field_info.annotation if len(query) == 1 else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        # FastAPI extracts a lone query model separately in each dependency.
        # Read its fields through Pydantic's public API, as FastAPI does.
        for name, info in annotation.model_fields.items():
            yield "query", _model_field_name(name, info), info
    else:
        for field in query:
            yield "query", _wire_name(field), field.field_info
    for child in dependant.dependencies:
        yield from _fields(child)


def _wire_name(field: Any) -> str:
    """The request name FastAPI reads for a path/query parameter field."""
    name: str = getattr(field, "validation_alias", None) or field.alias
    return name


def _model_field_name(name: str, info: FieldInfo) -> str:
    """The request name of a query model field: its string validation alias,
    else its alias, else its Python name."""
    if isinstance(info.validation_alias, str) and info.validation_alias:
        return info.validation_alias
    return info.alias or name
