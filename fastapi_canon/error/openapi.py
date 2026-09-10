from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI
from pydantic import ValidationError

from fastapi_canon.error.contracts import (
    ERRORS_EXTENSION,
    HTTP_STATUSES_EXTENSION,
    SUCCESS_EXTENSION,
    contracts_from_responses,
    iter_operations,
)
from fastapi_canon.error.problem import Problem
from fastapi_canon.error.types import (
    ErrorConfigurationError,
    JsonValue,
    OpenAPIResponse,
    OpenAPIResponses,
    http_problem_title,
)

if TYPE_CHECKING:
    from fastapi_canon.error.registry import AnyError, ErrorRegistry
    from fastapi_canon.response import CanonResponse

_PROBLEM_MEDIA_TYPE = "application/problem+json"
_MISSING = object()


def install_openapi(
    registry: ErrorRegistry,
    app: FastAPI,
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
) -> None:
    previous_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = previous_openapi()
        # FastAPI caches its document before our validation. Do not retain an
        # uncompiled schema if compilation fails.
        app.openapi_schema = None
        compiled = compile_document(
            registry,
            document,
            include_validation_error=include_validation_error,
            include_http_exceptions=include_http_exceptions,
        )
        app.openapi_schema = compiled
        return compiled

    cast(Any, app).openapi = openapi


def compile_document(
    registry: ErrorRegistry,
    document: dict[str, Any],
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    components = result.setdefault("components", {}).setdefault("schemas", {})
    _add_component(components, "Problem", _base_problem_schema())

    for error in registry.errors:
        _add_component(
            components,
            error.effective_schema_name,
            compile_error_schema(registry, error),
        )

    if include_validation_error:
        _add_component(
            components,
            "RequestValidationProblem",
            _request_validation_schema(registry),
        )

    for path, operation in iter_operations(result):
        responses = operation.setdefault("responses", {})
        _errors, http_statuses, _success = contracts_from_responses(
            path, responses, registry
        )
        if http_statuses and not include_http_exceptions:
            msg = (
                f"route {path!r} declares normalized HTTP responses, "
                "but HTTP exception normalization is disabled"
            )
            raise ErrorConfigurationError(msg)
        for status in http_statuses:
            _add_component(
                components, _http_component_name(status), _http_problem_schema(status)
            )
        for response in responses.values():
            if isinstance(response, dict):
                response.pop(ERRORS_EXTENSION, None)
                response.pop(HTTP_STATUSES_EXTENSION, None)
                success_media_type = response.pop(SUCCESS_EXTENSION, _MISSING)
                if success_media_type is not _MISSING:
                    _retain_success_content(response, success_media_type)
        if include_validation_error:
            _replace_default_validation_response(responses)

    return result


def compile_responses(
    registry: ErrorRegistry,
    errors: Sequence[AnyError],
    *,
    http_statuses: Sequence[int] = (),
    success: CanonResponse | None = None,
) -> OpenAPIResponses:
    grouped: dict[int, list[AnyError]] = {}
    for error in errors:
        if not registry.contains(error):
            msg = f"error {error.code!r} does not belong to this registry"
            raise ErrorConfigurationError(msg)
        grouped.setdefault(error.status, []).append(error)

    seen_http_statuses: set[int] = set()
    for index, status in enumerate(http_statuses):
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
        ):
            msg = f"http_statuses[{index}] must be between 400 and 599"
            raise ErrorConfigurationError(msg)
        if status in seen_http_statuses:
            msg = f"http_statuses[{index}] duplicates status {status}"
            raise ErrorConfigurationError(msg)
        conflicting_error = next(
            (
                error
                for error in grouped.get(status, ())
                if error.code == f"http_{status}"
            ),
            None,
        )
        if conflicting_error is not None:
            msg = (
                f"generic HTTP status {status} conflicts with domain error code "
                f"{conflicting_error.code!r}"
            )
            raise ErrorConfigurationError(msg)
        seen_http_statuses.add(status)

    statuses = dict.fromkeys((*grouped, *http_statuses))
    result: OpenAPIResponses = {
        status: _response_for_contracts(
            grouped.get(status, ()),
            http_status=status if status in seen_http_statuses else None,
        )
        for status in statuses
    }
    if success is not None:
        if success.status in result:
            msg = f"success response conflicts with error status {success.status}"
            raise ErrorConfigurationError(msg)
        return {**success.responses(), **result}
    return result


def _retain_success_content(response: dict[str, Any], media_type: object) -> None:
    if media_type is None or media_type == "":
        response.pop("content", None)
        return
    if not isinstance(media_type, str):
        msg = "compiled success response contains invalid media type metadata"
        raise ErrorConfigurationError(msg)
    content = response.get("content")
    if not isinstance(content, dict) or media_type not in content:
        msg = f"compiled success response is missing media type {media_type!r}"
        raise ErrorConfigurationError(msg)
    response["content"] = {media_type: content[media_type]}


def compile_error_schema(
    registry: ErrorRegistry,
    error: AnyError,
) -> dict[str, Any]:
    type_uri = registry.type_uri_for(error)
    if type_uri is None:
        msg = f"error {error.code!r} has no resolved problem type URI"
        raise ErrorConfigurationError(msg)
    schema = _base_problem_schema()
    schema["title"] = error.effective_schema_name
    properties = cast(dict[str, Any], schema["properties"])
    properties.update(
        {
            "type": {"type": "string", "format": "uri-reference", "const": type_uri},
            "title": {"type": "string", "const": error.title},
            "status": {"type": "integer", "const": error.status},
            "code": {"type": "string", "const": error.code},
        }
    )

    required = cast(list[str], schema["required"])
    if error.extensions_model is not None:
        extension_schema = error.extensions_model.model_json_schema(
            mode="serialization", by_alias=True
        )
        extension_schema = _rewrite_local_definitions(
            extension_schema, error.effective_schema_name
        )
        extension_properties = extension_schema.get("properties", {})
        properties.update(extension_properties)
        required.extend(extension_schema.get("required", []))
        if "$defs" in extension_schema:
            schema["$defs"] = extension_schema["$defs"]
    elif isinstance(error.extensions, Mapping):
        for name, value in error.extensions.items():
            properties[name] = _schema_for_static_value(value)
            required.append(name)

    if error.example is not None:
        example = _thaw(error.example)
        _validate_example(error, type_uri, example)
        schema["examples"] = [example]
    return schema


def _base_problem_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "title": "Problem",
        "description": "RFC 9457 Problem Details with a stable application code.",
        "properties": {
            "type": {"type": "string", "format": "uri-reference"},
            "title": {"type": "string"},
            "status": {"type": "integer", "minimum": 100, "maximum": 599},
            "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,}$"},
            "detail": {"type": ["string", "null"]},
            "instance": {"type": ["string", "null"], "format": "uri-reference"},
        },
        "required": ["type", "title", "status", "code"],
        "additionalProperties": True,
    }


def _response_for_contracts(
    errors: Sequence[AnyError], *, http_status: int | None
) -> OpenAPIResponse:
    references = [
        f"#/components/schemas/{error.effective_schema_name}" for error in errors
    ]
    titles = [error.title for error in errors]
    codes = [error.code for error in errors]
    if http_status is not None:
        references.append(f"#/components/schemas/{_http_component_name(http_status)}")
        titles.append(http_problem_title(http_status))
        codes.append(f"http_{http_status}")

    if len(references) == 1:
        schema: dict[str, Any] = {"$ref": references[0]}
        description = errors[0].description or errors[0].title if errors else titles[0]
    else:
        schema = {
            "oneOf": [{"$ref": reference} for reference in references],
            "discriminator": {
                "propertyName": "code",
                "mapping": {
                    code: reference
                    for code, reference in zip(codes, references, strict=True)
                },
            },
        }
        description = "Possible problems: " + ", ".join(titles)

    response: OpenAPIResponse = {
        "description": description,
        "content": {_PROBLEM_MEDIA_TYPE: {"schema": schema}},
    }
    if errors:
        response[ERRORS_EXTENSION] = [str(id(error)) for error in errors]
    if http_status is not None:
        response[HTTP_STATUSES_EXTENSION] = [http_status]
    headers: dict[str, Any] = {}
    for error in errors:
        for name, definition in (error.openapi_headers or {}).items():
            existing = next(
                (known for known in headers if known.lower() == name.lower()), None
            )
            candidate = _thaw(definition)
            if existing is not None and headers[existing] != candidate:
                msg = f"conflicting OpenAPI header {name!r} for status {error.status}"
                raise ErrorConfigurationError(msg)
            headers[name] = candidate
    if headers:
        response["headers"] = headers
    return response


def _http_component_name(status: int) -> str:
    return f"Http{status}Problem"


def _http_problem_schema(status: int) -> dict[str, Any]:
    schema = _base_problem_schema()
    schema["title"] = _http_component_name(status)
    properties = cast(dict[str, Any], schema["properties"])
    properties.update(
        {
            "type": {
                "type": "string",
                "format": "uri-reference",
                "const": "about:blank",
            },
            "title": {"type": "string", "const": http_problem_title(status)},
            "status": {"type": "integer", "const": status},
            "code": {"type": "string", "const": f"http_{status}"},
        }
    )
    return schema


def _merge_error_responses(
    target: dict[str, Any], generated: Mapping[int | str, dict[str, Any]]
) -> None:
    for status, response in generated.items():
        key = str(status)
        existing = target.get(key)
        if existing is None:
            target[key] = response
            continue
        content = existing.setdefault("content", {})
        if _PROBLEM_MEDIA_TYPE in content:
            msg = f"manual response {key} already defines {_PROBLEM_MEDIA_TYPE!r}"
            raise ErrorConfigurationError(msg)
        content[_PROBLEM_MEDIA_TYPE] = response["content"][_PROBLEM_MEDIA_TYPE]
        _merge_response_members(existing, response, key)


def _merge_response_members(
    existing: dict[str, Any], generated: dict[str, Any], status: str
) -> None:
    generated_headers = cast(dict[str, Any], generated.get("headers", {}))
    existing_headers = existing.setdefault("headers", {}) if generated_headers else {}
    for name, definition in generated_headers.items():
        collision = next(
            (known for known in existing_headers if known.lower() == name.lower()), None
        )
        if collision is not None and existing_headers[collision] != definition:
            msg = f"manual response {status} conflicts on header {name!r}"
            raise ErrorConfigurationError(msg)
        existing_headers[name] = definition


def _replace_default_validation_response(responses: dict[str, Any]) -> None:
    response = responses.get("422")
    if not isinstance(response, dict):
        return
    content = response.get("content")
    if not isinstance(content, dict):
        return
    application_json = content.get("application/json")
    if not isinstance(application_json, dict):
        return
    schema = application_json.get("schema")
    if schema != {"$ref": "#/components/schemas/HTTPValidationError"}:
        return
    content.pop("application/json")
    content[_PROBLEM_MEDIA_TYPE] = {
        "schema": {"$ref": "#/components/schemas/RequestValidationProblem"}
    }
    response["description"] = "Request validation failed"


def _request_validation_schema(registry: ErrorRegistry) -> dict[str, Any]:
    if registry.type_base is None:
        msg = "type_base is required for the request validation schema"
        raise ErrorConfigurationError(msg)
    schema = _base_problem_schema()
    properties = cast(dict[str, Any], schema["properties"])
    properties.update(
        {
            "type": {
                "type": "string",
                "format": "uri-reference",
                "const": f"{registry.type_base}/request_validation_error",
            },
            "title": {"type": "string", "const": "Request validation failed"},
            "status": {"type": "integer", "const": 422},
            "code": {"type": "string", "const": "request_validation_error"},
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "detail": {"type": "string"},
                        "pointer": {"type": "string"},
                        "parameter": {"type": "string"},
                        "in": {
                            "type": "string",
                            "enum": ["path", "query", "header", "cookie"],
                        },
                    },
                    "required": ["code", "detail"],
                    "additionalProperties": False,
                },
            },
        }
    )
    cast(list[str], schema["required"]).append("errors")
    schema["title"] = "RequestValidationProblem"
    return schema


def _add_component(
    components: dict[str, Any], name: str, schema: dict[str, Any]
) -> None:
    existing = components.get(name)
    if existing is not None and existing != schema:
        msg = f"OpenAPI component {name!r} already exists with another schema"
        raise ErrorConfigurationError(msg)
    components[name] = schema


def _rewrite_local_definitions(value: Any, component: str) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item.replace("#/$defs/", f"#/components/schemas/{component}/$defs/")
                if key == "$ref" and isinstance(item, str)
                else _rewrite_local_definitions(item, component)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_local_definitions(item, component) for item in value]
    return value


def _schema_for_static_value(value: JsonValue) -> dict[str, Any]:
    thawed = _thaw(value)
    return {"const": thawed, "type": _json_type(thawed)}


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _validate_example(error: AnyError, type_uri: str, example: dict[str, Any]) -> None:
    expected = {
        "type": type_uri,
        "title": error.title,
        "status": error.status,
        "code": error.code,
    }
    for name, value in expected.items():
        if example.get(name) != value:
            msg = f"example for error {error.code!r} has invalid {name!r}"
            raise ErrorConfigurationError(msg)
    if isinstance(error.extensions, Mapping):
        for name, value in error.extensions.items():
            if example.get(name) != _thaw(value):
                msg = (
                    f"example for error {error.code!r} has invalid extension "
                    f"member {name!r}"
                )
                raise ErrorConfigurationError(msg)
    try:
        Problem.model_validate(example)
        if error.extensions_model is not None:
            extension_values = {
                name: value
                for name, value in example.items()
                if name not in {"type", "title", "status", "code", "detail", "instance"}
            }
            error.extensions_model.model_validate(extension_values)
    except ValidationError as validation_error:
        msg = f"example for error {error.code!r} has invalid extension members"
        raise ErrorConfigurationError(msg) from validation_error


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
