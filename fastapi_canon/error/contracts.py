from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from fastapi_canon.error.types import ErrorConfigurationError

if TYPE_CHECKING:
    from fastapi_canon.error.registry import AnyError, ErrorRegistry

ERRORS_EXTENSION = "x-fastapi-canon-errors"
INSTALLED_REGISTRY_STATE_KEY = "_fastapi_canon_error_registry"


@runtime_checkable
class _IncludedRouterRoute(Protocol):
    original_router: APIRouter


def iter_http_contracts(
    router: APIRouter, registry: ErrorRegistry
) -> Iterator[tuple[APIRoute, tuple[AnyError, ...]]]:
    """Yield standard FastAPI routes and errors declared through responses=."""
    yield from _walk_http_contracts(router.routes, registry)


def _walk_http_contracts(
    routes: Sequence[BaseRoute], registry: ErrorRegistry
) -> Iterator[tuple[APIRoute, tuple[AnyError, ...]]]:
    for route in routes:
        if isinstance(route, _IncludedRouterRoute):
            yield from _walk_http_contracts(route.original_router.routes, registry)
            continue
        if isinstance(route, APIRoute):
            yield route, _errors_from_responses(route, registry)


def _errors_from_responses(
    route: APIRoute, registry: ErrorRegistry
) -> tuple[AnyError, ...]:
    by_identity = {str(id(error)): error for error in registry.errors}
    result: list[AnyError] = []
    seen: set[int] = set()

    for configured_response in route.responses.values():
        response: object = configured_response
        if not isinstance(response, Mapping):
            continue
        identities = response.get(ERRORS_EXTENSION, ())
        if not isinstance(identities, list) or not all(
            isinstance(identity, str) for identity in identities
        ):
            msg = f"route {route.path!r} contains invalid fastapi-canon error metadata"
            raise ErrorConfigurationError(msg)
        for identity in identities:
            error = by_identity.get(identity)
            if error is None:
                msg = (
                    f"route {route.path!r} declares an error whose exact definition "
                    "is missing from the installed registry"
                )
                raise ErrorConfigurationError(msg)
            if id(error) not in seen:
                seen.add(id(error))
                result.append(error)

    return tuple(result)
