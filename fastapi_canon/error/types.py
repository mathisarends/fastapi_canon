import re
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type OpenAPIHeader = Mapping[str, JsonValue]
type OpenAPIResponse = dict[str, JsonValue]
type OpenAPIResponses = dict[int | str, OpenAPIResponse]


class DetailRenderer[ExceptionT: Exception](Protocol):
    """Render occurrence-specific human-readable detail."""

    def __call__(self, exception: ExceptionT, /) -> str | None: ...


class ExtensionsRenderer[ExceptionT: Exception](Protocol):
    """Render typed or JSON-native Problem Details extension members."""

    def __call__(
        self, exception: ExceptionT, /
    ) -> Mapping[str, JsonValue] | BaseModel: ...


class HeadersRenderer[ExceptionT: Exception](Protocol):
    """Render response headers for an error occurrence."""

    def __call__(self, exception: ExceptionT, /) -> Mapping[str, str]: ...


type Detail[ExceptionT: Exception] = str | DetailRenderer[ExceptionT] | None
type Extensions[ExceptionT: Exception] = (
    Mapping[str, JsonValue] | ExtensionsRenderer[ExceptionT] | None
)
type Headers[ExceptionT: Exception] = (
    Mapping[str, str] | HeadersRenderer[ExceptionT] | None
)

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,}$")


class ErrorConfigurationError(ValueError):
    """Raised when error definitions cannot form a safe, coherent contract."""


def is_absolute_uri(value: str) -> bool:
    """Return whether *value* is a usable RFC 3986 absolute URI."""
    if not value or any(character.isspace() for character in value):
        return False

    try:
        parsed = urlsplit(value)
    except ValueError:
        return False

    if not parsed.scheme or parsed.fragment:
        return False
    return not (parsed.scheme in {"http", "https"} and not parsed.netloc)


def is_uri_reference(value: str) -> bool:
    """Return whether *value* is a non-empty RFC 3986 URI reference."""
    if not value or any(character.isspace() for character in value):
        return False
    try:
        urlsplit(value)
    except ValueError:
        return False
    return True
