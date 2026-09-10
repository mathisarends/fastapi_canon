from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Self, cast

from fastapi_canon.error.error import _freeze_json_mapping, _validate_header_name
from fastapi_canon.error.types import (
    ErrorConfigurationError,
    JsonValue,
    OpenAPIHeader,
    OpenAPIResponse,
    OpenAPIResponses,
)

type ResponseHeaders = Sequence[str] | Mapping[str, OpenAPIHeader]

_MEDIA_TYPE_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)
_NO_CONTENT_STATUSES = frozenset({204, 304})


@dataclass(frozen=True, slots=True, init=False)
class CanonResponse:
    """An immutable OpenAPI contract for one successful HTTP response."""

    status: int
    media_type: str | None
    schema: Mapping[str, JsonValue] | None = field(repr=False)
    description: str
    headers: Mapping[str, OpenAPIHeader] = field(repr=False)

    def __init__(
        self,
        *,
        media_type: str | None,
        status: int = 200,
        schema: Mapping[str, JsonValue] | None = None,
        description: str = "Successful response",
        headers: ResponseHeaders = (),
    ) -> None:
        normalized_status = _validate_status(status)
        normalized_media_type = _validate_media_type(media_type)
        normalized_description = _validate_description(description)
        normalized_schema = _normalize_schema(schema)
        normalized_headers = _normalize_headers(headers)
        if normalized_media_type is None and normalized_schema is not None:
            msg = "schema requires a media_type"
            raise ErrorConfigurationError(msg)
        if normalized_media_type is not None and status in _NO_CONTENT_STATUSES:
            msg = f"status {status} must not define response content"
            raise ErrorConfigurationError(msg)

        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(self, "media_type", normalized_media_type)
        object.__setattr__(self, "schema", normalized_schema)
        object.__setattr__(self, "description", normalized_description)
        object.__setattr__(self, "headers", normalized_headers)

    @classmethod
    def json(
        cls,
        schema: Mapping[str, JsonValue] | None = None,
        *,
        status: int = 200,
        description: str = "JSON response",
        headers: ResponseHeaders = (),
    ) -> Self:
        return cls(
            status=status,
            media_type="application/json",
            schema=schema,
            description=description,
            headers=headers,
        )

    @classmethod
    def empty(
        cls,
        *,
        status: int = 204,
        description: str = "No content",
        headers: ResponseHeaders = (),
    ) -> Self:
        return cls(
            status=status,
            media_type=None,
            description=description,
            headers=headers,
        )

    @classmethod
    def stream(
        cls,
        media_type: str,
        *,
        status: int = 200,
        schema: Mapping[str, JsonValue] | None = None,
        description: str = "Streaming response",
        headers: ResponseHeaders = (),
    ) -> Self:
        return cls(
            status=status,
            media_type=media_type,
            schema={"type": "string"} if schema is None else schema,
            description=description,
            headers=headers,
        )

    @classmethod
    def binary(
        cls,
        media_type: str,
        *,
        status: int = 200,
        schema: Mapping[str, JsonValue] | None = None,
        description: str = "Binary response",
        headers: ResponseHeaders = (),
    ) -> Self:
        return cls(
            status=status,
            media_type=media_type,
            schema=(
                {"type": "string", "format": "binary"} if schema is None else schema
            ),
            description=description,
            headers=headers,
        )

    @classmethod
    def sse(
        cls,
        *,
        status: int = 200,
        schema: Mapping[str, JsonValue] | None = None,
        description: str = "Server-sent event stream",
        headers: ResponseHeaders = (),
    ) -> Self:
        return cls.stream(
            "text/event-stream",
            status=status,
            schema=schema,
            description=description,
            headers=headers,
        )

    def as_openapi(self) -> OpenAPIResponse:
        """Return a detached FastAPI-compatible response declaration."""
        result: OpenAPIResponse = {"description": self.description}
        if self.media_type is not None:
            schema = _thaw(self.schema) if self.schema is not None else {}
            result["content"] = {self.media_type: {"schema": schema}}
        if self.headers:
            result["headers"] = {
                name: _thaw(definition) for name, definition in self.headers.items()
            }
        return result

    def responses(self) -> OpenAPIResponses:
        """Compile this contract for a FastAPI route's ``responses`` argument."""
        from fastapi_canon.error.contracts import SUCCESS_EXTENSION

        response = self.as_openapi()
        response[SUCCESS_EXTENSION] = self.media_type
        return {self.status: response}


def _validate_status(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 200 <= value <= 399:
        msg = "response status must be between 200 and 399"
        raise ErrorConfigurationError(msg)
    return value


def _validate_media_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _MEDIA_TYPE_PATTERN.fullmatch(value) is None:
        msg = "media_type must be a valid type/subtype string or None"
        raise ErrorConfigurationError(msg)
    return value.lower()


def _validate_description(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        msg = "response description must be a non-empty, single-line string"
        raise ErrorConfigurationError(msg)
    return value


def _normalize_schema(
    value: Mapping[str, JsonValue] | None,
) -> Mapping[str, JsonValue] | None:
    raw_value: object = value
    if raw_value is None:
        return None
    if not isinstance(raw_value, Mapping):
        msg = "schema must be a mapping or None"
        raise ErrorConfigurationError(msg)
    return _freeze_json_mapping(raw_value, path="response schema")


def _normalize_headers(value: ResponseHeaders) -> Mapping[str, OpenAPIHeader]:
    raw_value: object = value
    if isinstance(raw_value, str) or not isinstance(raw_value, Mapping | Sequence):
        msg = "response headers must be a sequence of names or a mapping"
        raise ErrorConfigurationError(msg)

    definitions: dict[str, OpenAPIHeader] = {}
    items: Sequence[tuple[object, object]]
    if isinstance(raw_value, Mapping):
        items = tuple(raw_value.items())
    else:
        items = tuple((name, {"schema": {"type": "string"}}) for name in raw_value)

    seen: set[str] = set()
    for name, definition in items:
        header_name = _validate_header_name(name, path="response headers")
        normalized_name = header_name.lower()
        if normalized_name in seen:
            msg = f"response headers contain duplicate name {header_name!r}"
            raise ErrorConfigurationError(msg)
        seen.add(normalized_name)
        if not isinstance(definition, Mapping):
            msg = f"response header {header_name!r} must be a mapping"
            raise ErrorConfigurationError(msg)
        definitions[header_name] = _freeze_json_mapping(
            definition,
            path=f"response headers.{header_name}",
        )
    return MappingProxyType(definitions)


def _thaw(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return cast(JsonValue, value)
