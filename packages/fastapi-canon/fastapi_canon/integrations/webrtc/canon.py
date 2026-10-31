"""Canon admission contracts for WebRTC sessions."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.params import Depends as DependsParam

from fastapi_canon.error.contracts import INSTALLED_REGISTRY_STATE_KEY
from fastapi_canon.error.openapi import compile_error_schema
from fastapi_canon.error.registry import AnyError, ErrorRegistry
from fastapi_canon.error.types import ErrorConfigurationError
from fastapi_canon.integrations.webrtc.declarations import (
    AdmissionErrors,
    DataChannelSpec,
    MediaPolicy,
    Role,
    SessionLimits,
)
from fastapi_canon.integrations.webrtc.router import WebRtcRouter
from fastapi_canon.policy import normalize_raises as _normalize_raises
from fastapi_canon.router import CanonRouter, _distinct

__all__ = ["CanonWebRtcRouter"]

Factory = TypeVar("Factory", bound=Callable[..., Any])


class CanonWebRtcRouter(CanonRouter, WebRtcRouter):
    """Reuse Canon errors for pre-accept HTTP admission contracts.

    Declare errors from dependencies and the factory with ``raises``. This
    documents failures and validates membership; it is not an exception allowlist.
    Once accepted, failures use WebRTC's protocol and lifecycle instead of HTTP.
    """

    def session(
        self,
        path: str,
        *,
        name: str | None = None,
        role: Role = Role.ANSWERER,
        media: MediaPolicy | None = None,
        data_channels: Sequence[DataChannelSpec] = (),
        limits: SessionLimits | None = None,
        max_sessions: int | None = None,
        dependencies: Sequence[DependsParam] | None = None,
        admission_errors: AdmissionErrors | None = None,
        raises: Sequence[AnyError] = (),
    ) -> Callable[[Factory], Factory]:
        if admission_errors is not None:
            msg = "CanonWebRtcRouter derives admission_errors from raises"
            raise ErrorConfigurationError(msg)
        declared = _distinct(
            (
                *self.raises,
                *_normalize_raises(
                    raises, registry=self.error_registry, parameter="raises"
                ),
            )
        )

        def contracts(app: FastAPI | None) -> Sequence[Mapping[str, Any]]:
            registry = self.error_registry
            if app is not None:
                registry = getattr(app.state, INSTALLED_REGISTRY_STATE_KEY, registry)
            if not declared:
                return []
            if not isinstance(registry, ErrorRegistry):
                raise ErrorConfigurationError(
                    "Admission errors require an ErrorRegistry"
                )
            result: list[Mapping[str, Any]] = []
            for error in declared:
                # Resolve against the composed registry, including its type_base.
                # Identity checks also catch features that explicitly omit errors.
                schema = compile_error_schema(registry, error)
                type_uri = registry.type_uri_for(error)
                prefix = f"#/components/schemas/{error.effective_schema_name}/"
                schema = _local_schema(schema, prefix)
                schema["$id"] = type_uri
                result.append(
                    {
                        "status": error.status,
                        "code": error.code,
                        "type": type_uri,
                        "title": error.title,
                        "description": error.description or error.title,
                        "media_type": "application/problem+json",
                        "schema": schema,
                    }
                )
            return result

        return super().session(
            path,
            name=name,
            role=role,
            media=media,
            data_channels=data_channels,
            limits=limits,
            max_sessions=max_sessions,
            dependencies=dependencies,
            admission_errors=contracts,
        )


def _local_schema(value: Any, prefix: str) -> Any:
    """Make the embedded schema a self-contained JSON Schema resource."""
    if isinstance(value, dict):
        return {
            key: (
                "#/" + item[len(prefix) :]
                if key == "$ref" and isinstance(item, str) and item.startswith(prefix)
                else _local_schema(item, prefix)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_local_schema(item, prefix) for item in value]
    return value
