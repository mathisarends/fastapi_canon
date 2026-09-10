from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import urlsplit

from fastapi import FastAPI

from fastapi_canon.error.error import Error
from fastapi_canon.error.handlers import install_handlers
from fastapi_canon.error.openapi import compile_responses
from fastapi_canon.error.types import (
    ErrorConfigurationError,
    OpenAPIResponses,
    is_absolute_uri,
)
from fastapi_canon.error.validation import unique_instances

type AnyError = Error[Any]


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    error: AnyError
    type_uri: str | None
    source: str


@dataclass(frozen=True, slots=True, init=False)
class ErrorRegistry:
    """An immutable, explicitly composable collection of error definitions."""

    name: str | None
    type_base: str | None
    _entries: tuple[_RegistryEntry, ...] = field(repr=False)
    _by_exception: Mapping[type[Exception], _RegistryEntry] = field(
        repr=False, compare=False, hash=False
    )

    def __init__(
        self,
        *,
        errors: Sequence[AnyError],
        name: str | None = None,
        type_base: str | None = None,
    ) -> None:
        normalized_name = _validate_name(name)
        normalized_base = _validate_type_base(type_base)
        source = normalized_name or "<anonymous>"
        entries = tuple(
            _RegistryEntry(
                error=error,
                type_uri=_resolve_type_uri(error, normalized_base),
                source=source,
            )
            for error in unique_instances(errors, Error, parameter="errors")
        )
        self._initialize(
            entries=entries,
            name=normalized_name,
            type_base=normalized_base,
        )

    @classmethod
    def merge(
        cls,
        *registries: Self,
        name: str | None = None,
        type_base: str | None = None,
    ) -> Self:
        """Compose registries and validate all collisions."""
        normalized_name = _validate_name(name)
        normalized_base = _validate_type_base(type_base)
        merged: dict[int, _RegistryEntry] = {}

        for registry in unique_instances(
            registries, ErrorRegistry, parameter="registries"
        ):
            for entry in registry._entries:
                identity = id(entry.error)
                previous = merged.get(identity)
                if previous is None:
                    merged[identity] = entry
                    continue
                if (
                    previous.type_uri is not None
                    and entry.type_uri is not None
                    and previous.type_uri != entry.type_uri
                ):
                    msg = (
                        f"error {entry.error.code!r} is shared by registries "
                        f"{previous.source!r} and {entry.source!r} with incompatible "
                        f"type URIs {previous.type_uri!r} and {entry.type_uri!r}"
                    )
                    raise ErrorConfigurationError(msg)
                if previous.type_uri is None and entry.type_uri is not None:
                    merged[identity] = replace(previous, type_uri=entry.type_uri)

        resolved = tuple(
            entry
            if entry.type_uri is not None or normalized_base is None
            else replace(
                entry,
                type_uri=_resolve_type_uri(entry.error, normalized_base),
            )
            for entry in merged.values()
        )
        instance = object.__new__(cls)
        instance._initialize(
            entries=resolved,
            name=normalized_name,
            type_base=normalized_base,
        )
        return instance

    @property
    def errors(self) -> tuple[AnyError, ...]:
        """Return definitions in deterministic declaration order."""
        return tuple(entry.error for entry in self._entries)

    def __iter__(self) -> Iterator[AnyError]:
        return iter(self.errors)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, exception: Exception) -> AnyError | None:
        """Resolve the most specific registered error through normal Python MRO."""
        for exception_class in type(exception).__mro__:
            entry = self._by_exception.get(exception_class)
            if entry is not None:
                return entry.error
        return None

    def type_uri_for(self, error: AnyError) -> str | None:
        """Return a member's resolved type URI, or None if still unresolved."""
        entry = self._by_exception.get(error.exception)
        if entry is None or entry.error is not error:
            msg = f"error {error.code!r} does not belong to this registry"
            raise ErrorConfigurationError(msg)
        return entry.type_uri

    def contains(self, error: AnyError) -> bool:
        """Check membership by definition identity."""
        entry = self._by_exception.get(error.exception)
        return entry is not None and entry.error is error

    def require_resolved(self) -> None:
        """Reject registries with unresolved problem type URIs."""
        unresolved = [
            entry.error.code for entry in self._entries if entry.type_uri is None
        ]
        if unresolved:
            codes = ", ".join(repr(code) for code in unresolved)
            msg = (
                "registry cannot be installed with unresolved problem type URIs: "
                f"{codes}; provide explicit Error.type values or a type_base"
            )
            raise ErrorConfigurationError(msg)

    def responses(
        self,
        *errors: AnyError,
        http_statuses: Sequence[int] = (),
    ) -> OpenAPIResponses:
        """Compile domain and normalized HTTP responses for FastAPI routes."""
        return compile_responses(self, errors, http_statuses=http_statuses)

    def install(
        self,
        app: FastAPI,
        *,
        include_validation_error: bool = True,
        include_http_exceptions: bool = True,
        include_unhandled_error: bool = True,
    ) -> None:
        """Install runtime handlers and OpenAPI integration."""
        install_handlers(
            self,
            app,
            include_validation_error=include_validation_error,
            include_http_exceptions=include_http_exceptions,
            include_unhandled_error=include_unhandled_error,
        )

    def _initialize(
        self,
        *,
        entries: tuple[_RegistryEntry, ...],
        name: str | None,
        type_base: str | None,
    ) -> None:
        by_exception = _validate_collisions(entries)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type_base", type_base)
        object.__setattr__(self, "_entries", entries)
        object.__setattr__(self, "_by_exception", MappingProxyType(by_exception))


def _validate_name(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        msg = "registry name must be a non-empty, single-line string or None"
        raise ErrorConfigurationError(msg)
    return value


def _validate_type_base(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not is_absolute_uri(value):
        msg = "type_base must be an absolute URI without a fragment"
        raise ErrorConfigurationError(msg)
    parsed = urlsplit(value)
    if parsed.query:
        msg = "type_base must not contain a query string"
        raise ErrorConfigurationError(msg)
    return value.rstrip("/")


def _resolve_type_uri(error: AnyError, type_base: str | None) -> str | None:
    if error.type is not None:
        return error.type
    if type_base is None:
        return None
    return f"{type_base}/{error.code}"


def _validate_collisions(
    entries: tuple[_RegistryEntry, ...],
) -> dict[type[Exception], _RegistryEntry]:
    dimensions: tuple[tuple[str, dict[object, _RegistryEntry]], ...] = (
        ("exception class", {}),
        ("code", {}),
        ("type URI", {}),
        ("schema name", {}),
    )
    by_exception: dict[type[Exception], _RegistryEntry] = {}

    for entry in entries:
        keys: tuple[object | None, ...] = (
            entry.error.exception,
            entry.error.code,
            entry.type_uri,
            entry.error.effective_schema_name,
        )
        for (dimension, seen), key in zip(dimensions, keys, strict=True):
            if key is None:
                continue
            previous = seen.get(key)
            if previous is not None and previous.error is not entry.error:
                raise _collision_error(dimension, key, previous, entry)
            seen[key] = entry
        by_exception[entry.error.exception] = entry

    return by_exception


def _collision_error(
    dimension: str,
    value: object,
    first: _RegistryEntry,
    second: _RegistryEntry,
) -> ErrorConfigurationError:
    return ErrorConfigurationError(
        f"conflicting {dimension} {value!r}: error {first.error.code!r} from "
        f"registry {first.source!r} conflicts with error {second.error.code!r} "
        f"from registry {second.source!r}"
    )
