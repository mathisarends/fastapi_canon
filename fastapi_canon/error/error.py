import builtins
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from pydantic import BaseModel, ValidationError

from fastapi_canon.error.types import (
    CODE_PATTERN,
    Detail,
    ErrorConfigurationError,
    Extensions,
    Headers,
    JsonValue,
    OpenAPIHeader,
    is_absolute_uri,
)

_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "code"})
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_RESPONSE_HEADERS = frozenset(
    {"connection", "content-length", "content-type", "transfer-encoding"}
)


@dataclass(frozen=True, slots=True, eq=False)
class Error[ExceptionT: Exception]:
    """An immutable error contract shared by runtime handling and OpenAPI."""

    exception: builtins.type[ExceptionT]
    status: int = field(kw_only=True)
    code: str = field(kw_only=True)
    title: str = field(kw_only=True)
    type: str | None = field(default=None, kw_only=True)
    detail: Detail[ExceptionT] = field(default=None, kw_only=True)
    extensions_model: builtins.type[BaseModel] | None = field(
        default=None, kw_only=True
    )
    extensions: Extensions[ExceptionT] = field(default=None, kw_only=True)
    headers: Headers[ExceptionT] = field(default=None, kw_only=True)
    openapi_headers: Mapping[str, OpenAPIHeader] | None = field(
        default=None, kw_only=True
    )
    description: str | None = field(default=None, kw_only=True)
    example: Mapping[str, JsonValue] | None = field(default=None, kw_only=True)
    schema_name: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Validate and detach all static inputs from caller-owned mutation."""
        self._validate_identity()
        self._validate_detail()
        self._validate_extensions()
        self._validate_headers()
        self._validate_documentation()

    @property
    def effective_schema_name(self) -> str:
        """Return the explicit or deterministic OpenAPI component name."""
        if self.schema_name is not None:
            return self.schema_name
        return "".join(part.capitalize() for part in self.code.split("_")) + "Problem"

    def _validate_identity(self) -> None:
        exception: object = self.exception
        status: object = self.status
        code: object = self.code
        title: object = self.title
        problem_type: object = self.type

        if not isinstance(exception, builtins.type) or not issubclass(
            exception, Exception
        ):
            msg = "exception must be an Exception subclass"
            raise ErrorConfigurationError(msg)
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 400 <= status <= 599
        ):
            msg = "status must be between 400 and 599"
            raise ErrorConfigurationError(msg)
        if not isinstance(code, str) or CODE_PATTERN.fullmatch(code) is None:
            msg = "code must match ^[a-z][a-z0-9_]{2,}$"
            raise ErrorConfigurationError(msg)
        if (
            not isinstance(title, str)
            or not title.strip()
            or "\r" in title
            or "\n" in title
        ):
            msg = "title must be a non-empty, single-line string"
            raise ErrorConfigurationError(msg)
        if problem_type is not None and (
            not isinstance(problem_type, str) or not is_absolute_uri(problem_type)
        ):
            msg = "type must be an absolute URI without a fragment"
            raise ErrorConfigurationError(msg)

    def _validate_detail(self) -> None:
        detail: object = self.detail
        if detail is not None and not isinstance(detail, str) and not callable(detail):
            msg = "detail must be a string, callable, or None"
            raise ErrorConfigurationError(msg)

    def _validate_extensions(self) -> None:
        model: object = self.extensions_model
        extensions: object = self.extensions

        if model is not None and (
            not isinstance(model, builtins.type) or not issubclass(model, BaseModel)
        ):
            msg = "extensions_model must be a Pydantic BaseModel subclass"
            raise ErrorConfigurationError(msg)
        if model is not None:
            self._validate_extension_model(model)

        if extensions is None:
            if model is not None:
                msg = "extensions is required when extensions_model is provided"
                raise ErrorConfigurationError(msg)
            return

        if not isinstance(extensions, Mapping):
            if not callable(extensions):
                msg = "extensions must be a mapping, callable, or None"
                raise ErrorConfigurationError(msg)
            if model is None:
                msg = "callable extensions require extensions_model"
                raise ErrorConfigurationError(msg)
            return

        overlap = _RESERVED_MEMBERS.intersection(extensions)
        if overlap:
            names = ", ".join(sorted(overlap))
            msg = f"extensions must not redefine reserved members: {names}"
            raise ErrorConfigurationError(msg)

        frozen_extensions = _freeze_json_mapping(extensions, path="extensions")
        if model is not None:
            try:
                validated = model.model_validate(dict(frozen_extensions))
            except ValidationError as error:
                msg = "static extensions do not validate against extensions_model"
                raise ErrorConfigurationError(msg) from error
            serialized = cast(
                dict[str, JsonValue],
                validated.model_dump(mode="json", by_alias=True),
            )
            frozen_extensions = _freeze_json_mapping(serialized, path="extensions")
        object.__setattr__(self, "extensions", frozen_extensions)

    def _validate_extension_model(self, model: builtins.type[BaseModel]) -> None:
        serialized_names = {
            field.serialization_alias or field.alias or name
            for name, field in model.model_fields.items()
        }
        overlap = _RESERVED_MEMBERS.intersection(serialized_names)
        if overlap:
            names = ", ".join(sorted(overlap))
            msg = f"extensions_model must not define reserved members: {names}"
            raise ErrorConfigurationError(msg)

    def _validate_headers(self) -> None:
        headers: object = self.headers
        openapi_headers: object = self.openapi_headers
        if (
            headers is not None
            and not isinstance(headers, Mapping)
            and not callable(headers)
        ):
            msg = "headers must be a mapping, callable, or None"
            raise ErrorConfigurationError(msg)
        if isinstance(headers, Mapping):
            frozen_headers = freeze_headers(headers, path="headers")
            object.__setattr__(self, "headers", frozen_headers)

        if openapi_headers is not None:
            if not isinstance(openapi_headers, Mapping):
                msg = "openapi_headers must be a mapping or None"
                raise ErrorConfigurationError(msg)
            definitions: dict[str, OpenAPIHeader] = {}
            for name, definition in openapi_headers.items():
                header_name = _validate_header_name(name, path="openapi_headers")
                if not isinstance(definition, Mapping):
                    msg = f"openapi_headers.{header_name} must be a mapping"
                    raise ErrorConfigurationError(msg)
                definitions[header_name] = _freeze_json_mapping(
                    definition, path=f"openapi_headers.{header_name}"
                )
            object.__setattr__(self, "openapi_headers", MappingProxyType(definitions))

    def _validate_documentation(self) -> None:
        description: object = self.description
        schema_name: object = self.schema_name
        example: object = self.example
        if description is not None and not isinstance(description, str):
            msg = "description must be a string or None"
            raise ErrorConfigurationError(msg)
        if schema_name is not None and (
            not isinstance(schema_name, str) or not schema_name.strip()
        ):
            msg = "schema_name must be a non-empty string or None"
            raise ErrorConfigurationError(msg)
        if example is not None:
            if not isinstance(example, Mapping):
                msg = "example must be a mapping or None"
                raise ErrorConfigurationError(msg)
            object.__setattr__(
                self,
                "example",
                _freeze_json_mapping(example, path="example"),
            )


def _freeze_json_mapping[KeyT, ValueT](
    value: Mapping[KeyT, ValueT], *, path: str
) -> Mapping[str, JsonValue]:
    frozen: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            msg = f"{path} keys must be strings"
            raise ErrorConfigurationError(msg)
        frozen[key] = _freeze_json(item, path=f"{path}.{key}")
    return MappingProxyType(frozen)


def _freeze_json(value: object, *, path: str) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f"{path} must contain only finite JSON numbers"
            raise ErrorConfigurationError(msg)
        return value
    if isinstance(value, list):
        return cast(
            JsonValue,
            tuple(
                _freeze_json(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)
            ),
        )
    if isinstance(value, Mapping):
        return cast(JsonValue, _freeze_json_mapping(value, path=path))
    msg = f"{path} contains a non-JSON value of type {type(value).__name__}"
    raise ErrorConfigurationError(msg)


def freeze_headers[KeyT, ValueT](
    value: Mapping[KeyT, ValueT], *, path: str
) -> Mapping[str, str]:
    """Validate response headers and return an immutable detached mapping."""
    frozen: dict[str, str] = {}
    for name, header_value in value.items():
        header_name = _validate_header_name(name, path=path)
        if not isinstance(header_value, str):
            msg = f"{path}.{header_name} must be a string"
            raise ErrorConfigurationError(msg)
        frozen[header_name] = header_value
    return MappingProxyType(frozen)


def _validate_header_name(name: object, *, path: str) -> str:
    if not isinstance(name, str) or _HEADER_NAME_PATTERN.fullmatch(name) is None:
        msg = f"{path} contains an invalid HTTP header name"
        raise ErrorConfigurationError(msg)
    if name.lower() in _FORBIDDEN_RESPONSE_HEADERS:
        msg = f"{path} must not define the managed response header {name!r}"
        raise ErrorConfigurationError(msg)
    return name
