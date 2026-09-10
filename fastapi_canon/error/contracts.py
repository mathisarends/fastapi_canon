from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from fastapi_canon.error.types import ErrorConfigurationError

if TYPE_CHECKING:
    from fastapi_canon.error.registry import AnyError, ErrorRegistry

ERRORS_EXTENSION = "x-fastapi-canon-errors"
HTTP_STATUSES_EXTENSION = "x-fastapi-canon-http-statuses"
SUCCESS_EXTENSION = "x-fastapi-canon-success"
INSTALLED_REGISTRY_STATE_KEY = "_fastapi_canon_error_registry"


HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def iter_operations(
    document: Mapping[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Read operations from the generated OpenAPI document, never router objects."""
    for section in ("paths", "webhooks"):
        for path, item in document.get(section, {}).items():
            if not isinstance(item, dict):
                continue
            for method, operation in item.items():
                if method in HTTP_METHODS and isinstance(operation, dict):
                    yield f"{method.upper()} {path}", operation


def contracts_from_responses(
    path: str, responses: Mapping[str, Any], registry: ErrorRegistry
) -> tuple[
    tuple[AnyError, ...],
    tuple[int, ...],
    tuple[int, str | None] | None,
]:
    by_identity = {str(id(error)): error for error in registry.errors}
    result: list[AnyError] = []
    seen: set[int] = set()
    http_statuses: list[int] = []
    seen_http_statuses: set[int] = set()
    success: tuple[int, str | None] | None = None

    for configured_status, configured_response in responses.items():
        response: object = configured_response
        if not isinstance(response, Mapping):
            continue
        if ERRORS_EXTENSION in response:
            identities = response[ERRORS_EXTENSION]
            if not isinstance(identities, list) or not all(
                isinstance(identity, str) for identity in identities
            ):
                msg = f"route {path!r} contains invalid fastapi-canon error metadata"
                raise ErrorConfigurationError(msg)
            for identity in identities:
                error = by_identity.get(identity)
                if error is None:
                    msg = (
                        f"route {path!r} declares an error whose exact "
                        "definition is missing from the installed registry"
                    )
                    raise ErrorConfigurationError(msg)
                if id(error) not in seen:
                    seen.add(id(error))
                    result.append(error)

        if HTTP_STATUSES_EXTENSION in response:
            statuses = response[HTTP_STATUSES_EXTENSION]
            if not isinstance(statuses, list) or not all(
                isinstance(status, int)
                and not isinstance(status, bool)
                and 400 <= status <= 599
                for status in statuses
            ):
                msg = (
                    f"route {path!r} contains invalid fastapi-canon HTTP "
                    "status metadata"
                )
                raise ErrorConfigurationError(msg)
            for status in statuses:
                if status not in seen_http_statuses:
                    seen_http_statuses.add(status)
                    http_statuses.append(status)

        if SUCCESS_EXTENSION in response:
            media_type = response[SUCCESS_EXTENSION]
            if media_type is not None and not isinstance(media_type, str):
                msg = f"route {path!r} contains invalid fastapi-canon success metadata"
                raise ErrorConfigurationError(msg)
            try:
                status = int(configured_status)
            except (TypeError, ValueError) as error:
                msg = f"route {path!r} has a non-numeric success status"
                raise ErrorConfigurationError(msg) from error
            if success is not None:
                msg = f"route {path!r} declares multiple success contracts"
                raise ErrorConfigurationError(msg)
            success = (status, media_type or None)

    return tuple(result), tuple(http_statuses), success
