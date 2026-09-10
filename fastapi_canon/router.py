from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter

from fastapi_canon.error.error import Error
from fastapi_canon.error.registry import AnyError, ErrorRegistry
from fastapi_canon.error.types import ErrorConfigurationError, OpenAPIResponses
from fastapi_canon.error.validation import unique_instances
from fastapi_canon.response import CanonResponse, ResponseConfigurationError


class CanonRouter(APIRouter):
    """An APIRouter that composes shared and operation response contracts."""

    def __init__(
        self,
        *,
        error_registry: ErrorRegistry | None = None,
        raises: Sequence[AnyError] = (),
        **kwargs: Any,
    ) -> None:
        raw_error_registry: object = error_registry
        if raw_error_registry is not None and not isinstance(
            raw_error_registry, ErrorRegistry
        ):
            msg = "error_registry must be an ErrorRegistry instance or None"
            raise ErrorConfigurationError(msg)
        self.error_registry = error_registry
        self.raises = _normalize_raises(
            raises,
            registry=error_registry,
            parameter="raises",
        )
        super().__init__(**kwargs)

    def api_route[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        """Declare a route and compile its inherited and local contracts."""
        local_raises = _normalize_raises(
            raises,
            registry=self.error_registry,
            parameter="raises",
        )
        raw_response: object = response
        if raw_response is not None and not isinstance(raw_response, CanonResponse):
            msg = "response must be a fastapi_canon.CanonResponse instance or None"
            raise ResponseConfigurationError(msg)

        status_code = kwargs.get("status_code")
        if response is not None:
            if response.media_type is None and kwargs.get("response_model") is not None:
                msg = f"route {path!r} response_model conflicts with bodyless CanonResponse"
                raise ResponseConfigurationError(msg)
            if status_code is None:
                kwargs["status_code"] = response.status
            elif status_code != response.status:
                msg = (
                    f"route {path!r} declares response status {response.status}, but "
                    f"status_code is {status_code}"
                )
                raise ResponseConfigurationError(msg)

        configured_responses = kwargs.pop("responses", None)
        kwargs["responses"] = self._responses_for_route(
            local_raises,
            response=response,
            configured=configured_responses,
        )
        return super().api_route(path, **kwargs)

    def get[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("GET", path, raises, response, kwargs)

    def put[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("PUT", path, raises, response, kwargs)

    def post[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("POST", path, raises, response, kwargs)

    def delete[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("DELETE", path, raises, response, kwargs)

    def options[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("OPTIONS", path, raises, response, kwargs)

    def head[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("HEAD", path, raises, response, kwargs)

    def patch[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("PATCH", path, raises, response, kwargs)

    def trace[CallableT: Callable[..., Any]](
        self,
        path: str,
        *,
        raises: Sequence[AnyError] = (),
        response: CanonResponse | None = None,
        **kwargs: Any,
    ) -> Callable[[CallableT], CallableT]:
        return self._method_route("TRACE", path, raises, response, kwargs)

    def _method_route[CallableT: Callable[..., Any]](
        self,
        method: str,
        path: str,
        raises: Sequence[AnyError],
        response: CanonResponse | None,
        kwargs: dict[str, Any],
    ) -> Callable[[CallableT], CallableT]:
        return self.api_route(
            path,
            methods=[method],
            raises=raises,
            response=response,
            **kwargs,
        )

    def _responses_for_route(
        self,
        local_raises: tuple[AnyError, ...],
        *,
        response: CanonResponse | None,
        configured: object,
    ) -> OpenAPIResponses:
        manual = _normalize_responses(configured)
        declared = _distinct((*self.raises, *local_raises))
        if declared:
            registry = self.error_registry
            if registry is None:  # pragma: no cover - guarded by _normalize_raises
                raise AssertionError("raises require an error registry")
            canon = registry.responses(*declared, success=response)
        elif response is not None:
            canon = response.responses()
        else:
            return manual

        inherited_statuses = {_status_key(status) for status in self.responses}
        manual_statuses = {_status_key(status) for status in manual}
        for status in canon:
            normalized = _status_key(status)
            if normalized in inherited_statuses or normalized in manual_statuses:
                msg = (
                    f"Canon response status {status} conflicts with a FastAPI "
                    "responses declaration"
                )
                raise ResponseConfigurationError(msg)
        return {**manual, **canon}


def _normalize_raises(
    values: Sequence[AnyError],
    *,
    registry: ErrorRegistry | None,
    parameter: str,
) -> tuple[AnyError, ...]:
    raw_values: object = values
    if isinstance(raw_values, str) or not isinstance(raw_values, Sequence):
        msg = f"{parameter} must be a sequence of Error instances"
        raise ErrorConfigurationError(msg)
    normalized = tuple(unique_instances(values, Error, parameter=parameter))
    if normalized and registry is None:
        msg = f"{parameter} requires an error registry"
        raise ErrorConfigurationError(msg)
    if registry is not None:
        for error in normalized:
            if not registry.contains(error):
                msg = f"error {error.code!r} does not belong to this router's registry"
                raise ErrorConfigurationError(msg)
    return normalized


def _normalize_responses(value: object) -> OpenAPIResponses:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        msg = "responses must be a mapping or None"
        raise ResponseConfigurationError(msg)
    return dict(value)


def _distinct(errors: Sequence[AnyError]) -> tuple[AnyError, ...]:
    return tuple(unique_instances(errors, Error, parameter="raises"))


def _status_key(value: object) -> str:
    return str(value)
