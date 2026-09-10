from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dishka import AsyncContainer, Provider, make_async_container
from dishka.exceptions import DishkaError  # type: ignore[attr-defined]  # not exported
from dishka.integrations.fastapi import setup_dishka
from fastapi import APIRouter, FastAPI
from fastapi_faults import FaultConfigurationError, FaultRegistry
from starlette.types import ExceptionHandler, Lifespan

type FeatureLifespan = Lifespan[FastAPI]

_INSTALLATION_STATE_KEY = "_fastapi_canon_installation"


class FeatureConfigurationError(FaultConfigurationError):
    """Raised when feature contributions cannot be installed coherently."""


@dataclass(frozen=True, slots=True, init=False)
class ExceptionHandlerSpec:
    """Associate an exception class with a Starlette exception handler."""

    exception: type[Exception]
    handler: ExceptionHandler

    def __init__(self, exception: type[Exception], handler: ExceptionHandler) -> None:
        raw_exception: object = exception
        raw_handler: object = handler
        if not isinstance(raw_exception, type) or not issubclass(
            raw_exception, Exception
        ):
            msg = "exception must be an Exception subclass"
            raise FeatureConfigurationError(msg)
        if not callable(raw_handler):
            msg = "handler must be callable"
            raise FeatureConfigurationError(msg)
        object.__setattr__(self, "exception", raw_exception)
        object.__setattr__(self, "handler", raw_handler)


@dataclass(frozen=True, slots=True, init=False)
class Feature:
    """An immutable declaration of one feature's FastAPI contributions."""

    routers: tuple[APIRouter, ...]
    providers: tuple[Provider, ...]
    faults: FaultRegistry | None
    exception_handlers: tuple[ExceptionHandlerSpec, ...]
    lifespan: FeatureLifespan | None

    def __init__(
        self,
        *,
        routers: Sequence[APIRouter] = (),
        providers: Sequence[Provider] = (),
        faults: FaultRegistry | None = None,
        exception_handlers: Sequence[ExceptionHandlerSpec] = (),
        lifespan: FeatureLifespan | None = None,
    ) -> None:
        normalized_routers = _unique_instances(routers, APIRouter, "routers")
        normalized_providers = _unique_instances(providers, Provider, "providers")
        normalized_handlers = _unique_instances(
            exception_handlers,
            ExceptionHandlerSpec,
            "exception_handlers",
        )
        raw_faults: object = faults
        raw_lifespan: object = lifespan
        if raw_faults is not None and not isinstance(raw_faults, FaultRegistry):
            msg = "faults must be a FaultRegistry instance or None"
            raise FeatureConfigurationError(msg)
        if raw_lifespan is not None and not callable(raw_lifespan):
            msg = "lifespan must be callable or None"
            raise FeatureConfigurationError(msg)

        object.__setattr__(self, "routers", normalized_routers)
        object.__setattr__(self, "providers", normalized_providers)
        object.__setattr__(self, "faults", faults)
        object.__setattr__(self, "exception_handlers", normalized_handlers)
        object.__setattr__(self, "lifespan", lifespan)


@dataclass(frozen=True, slots=True, init=False)
class FaultOptions:
    """Application-wide options for composing feature fault registries."""

    type_base: str | None
    registry_name: str | None
    include_validation_error: bool
    include_http_exceptions: bool
    include_unhandled_error: bool

    def __init__(
        self,
        *,
        type_base: str | None = None,
        registry_name: str | None = "application",
        include_validation_error: bool = True,
        include_http_exceptions: bool = True,
        include_unhandled_error: bool = True,
    ) -> None:
        _validate_optional_string(type_base, "type_base")
        _validate_optional_string(registry_name, "registry_name")
        _validate_bool(include_validation_error, "include_validation_error")
        _validate_bool(include_http_exceptions, "include_http_exceptions")
        _validate_bool(include_unhandled_error, "include_unhandled_error")
        object.__setattr__(self, "type_base", type_base)
        object.__setattr__(self, "registry_name", registry_name)
        object.__setattr__(
            self,
            "include_validation_error",
            include_validation_error,
        )
        object.__setattr__(self, "include_http_exceptions", include_http_exceptions)
        object.__setattr__(self, "include_unhandled_error", include_unhandled_error)


@dataclass(frozen=True, slots=True, init=False)
class Composition:
    """An ordered, reusable declaration of an application's features."""

    features: tuple[Feature, ...]
    faults: FaultOptions

    def __init__(
        self,
        *features: Feature,
        faults: FaultOptions | None = None,
    ) -> None:
        normalized_features = _unique_instances(features, Feature, "features")
        raw_faults: object = faults
        if raw_faults is not None and not isinstance(raw_faults, FaultOptions):
            msg = "faults must be a FaultOptions instance or None"
            raise FeatureConfigurationError(msg)
        object.__setattr__(self, "features", normalized_features)
        object.__setattr__(self, "faults", faults or FaultOptions())

    def apply(self, app: FastAPI) -> FastAPI:
        """Apply this composition to *app* exactly once and return the app."""
        _apply_composition(app, self)
        return app


@dataclass(frozen=True, slots=True)
class _Installation:
    features: tuple[Feature, ...] = field(compare=False)
    feature_ids: tuple[int, ...]
    faults: FaultOptions
    container: AsyncContainer | None = field(compare=False)


def _apply_composition(app: FastAPI, composition: Composition) -> None:
    raw_app: object = app
    if not isinstance(raw_app, FastAPI):
        msg = "app must be a FastAPI instance"
        raise FeatureConfigurationError(msg)
    requested = _Installation(
        features=composition.features,
        feature_ids=tuple(id(feature) for feature in composition.features),
        faults=composition.faults,
        container=None,
    )
    installed = getattr(app.state, _INSTALLATION_STATE_KEY, None)
    if installed is not None:
        if not isinstance(installed, _Installation) or installed != requested:
            msg = "a different composition is already applied to this application"
            raise FeatureConfigurationError(msg)
        return

    routers = _flatten_routers(composition.features)
    providers = _flatten_providers(composition.features)
    handler_specs = _flatten_handlers(composition.features)
    lifespans = tuple(
        feature.lifespan
        for feature in composition.features
        if feature.lifespan is not None
    )
    _reject_duplicate_contributions(routers, "router")
    _reject_duplicate_contributions(providers, "provider")
    _validate_handlers(app, handler_specs)

    registries = tuple(
        feature.faults for feature in composition.features if feature.faults is not None
    )
    registry = _merge_faults(
        registries,
        name=composition.faults.registry_name,
        type_base=composition.faults.type_base,
    )
    container = _make_container(providers)
    _validate_fault_installation(
        app,
        routers,
        handler_specs,
        registry,
        include_validation_error=composition.faults.include_validation_error,
        include_http_exceptions=composition.faults.include_http_exceptions,
        include_unhandled_error=composition.faults.include_unhandled_error,
    )
    if container is not None and app.middleware_stack is not None:
        msg = "features with providers must be installed before the app starts"
        raise FeatureConfigurationError(msg)

    original_lifespan = app.router.lifespan_context
    for router in routers:
        app.include_router(router)
    for spec in handler_specs:
        app.add_exception_handler(spec.exception, spec.handler)
    if registry is not None:
        registry.install(
            app,
            include_validation_error=composition.faults.include_validation_error,
            include_http_exceptions=composition.faults.include_http_exceptions,
            include_unhandled_error=composition.faults.include_unhandled_error,
        )
    if container is not None:
        setup_dishka(container, app)
    if lifespans or container is not None:
        app.router.lifespan_context = _compose_lifespan(
            original_lifespan,
            lifespans,
            container,
        )

    completed = _Installation(
        features=composition.features,
        feature_ids=requested.feature_ids,
        faults=composition.faults,
        container=container,
    )
    setattr(app.state, _INSTALLATION_STATE_KEY, completed)


def _unique_instances[ItemT](
    values: Sequence[object], expected_type: type[ItemT], parameter: str
) -> tuple[ItemT, ...]:
    result: list[ItemT] = []
    seen: set[int] = set()
    for index, value in enumerate(values):
        if not isinstance(value, expected_type):
            msg = f"{parameter}[{index}] must be a {expected_type.__name__} instance"
            raise FeatureConfigurationError(msg)
        if id(value) in seen:
            msg = f"{parameter}[{index}] duplicates an earlier instance"
            raise FeatureConfigurationError(msg)
        seen.add(id(value))
        result.append(value)
    return tuple(result)


def _validate_optional_string(value: object, parameter: str) -> None:
    if value is not None and not isinstance(value, str):
        msg = f"{parameter} must be a string or None"
        raise FeatureConfigurationError(msg)


def _validate_bool(value: object, parameter: str) -> None:
    if not isinstance(value, bool):
        msg = f"{parameter} must be a bool"
        raise FeatureConfigurationError(msg)


def _flatten_routers(features: tuple[Feature, ...]) -> tuple[APIRouter, ...]:
    return tuple(router for feature in features for router in feature.routers)


def _flatten_providers(features: tuple[Feature, ...]) -> tuple[Provider, ...]:
    return tuple(provider for feature in features for provider in feature.providers)


def _flatten_handlers(
    features: tuple[Feature, ...],
) -> tuple[ExceptionHandlerSpec, ...]:
    return tuple(
        handler for feature in features for handler in feature.exception_handlers
    )


def _reject_duplicate_contributions(values: Sequence[object], kind: str) -> None:
    seen: set[int] = set()
    for value in values:
        if id(value) in seen:
            msg = f"the same {kind} instance is contributed by multiple features"
            raise FeatureConfigurationError(msg)
        seen.add(id(value))


def _validate_handlers(app: FastAPI, specs: tuple[ExceptionHandlerSpec, ...]) -> None:
    seen: set[type[Exception]] = set()
    for spec in specs:
        if spec.exception in seen:
            msg = (
                "multiple features define an exception handler for "
                f"{spec.exception.__qualname__}"
            )
            raise FeatureConfigurationError(msg)
        seen.add(spec.exception)
        if spec.exception in app.exception_handlers:
            msg = (
                "application already defines an exception handler for "
                f"{spec.exception.__qualname__}"
            )
            raise FeatureConfigurationError(msg)


def _merge_faults(
    registries: tuple[FaultRegistry, ...], *, name: str | None, type_base: str | None
) -> FaultRegistry | None:
    if not registries:
        return None
    inferred_base = type_base
    registry_bases = {registry.type_base for registry in registries}
    if inferred_base is None and len(registry_bases) == 1:
        inferred_base = next(iter(registry_bases))
    try:
        return FaultRegistry.merge(
            *registries,
            name=name,
            type_base=inferred_base,
        )
    except FaultConfigurationError as error:
        raise FeatureConfigurationError(str(error)) from error


def _make_container(providers: tuple[Provider, ...]) -> AsyncContainer | None:
    if not providers:
        return None
    try:
        return make_async_container(*providers)
    except DishkaError as error:
        msg = f"invalid Dishka provider graph: {error}"
        raise FeatureConfigurationError(msg) from error


def _validate_fault_installation(
    app: FastAPI,
    routers: tuple[APIRouter, ...],
    handler_specs: tuple[ExceptionHandlerSpec, ...],
    registry: FaultRegistry | None,
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
    include_unhandled_error: bool,
) -> None:
    if registry is None:
        return
    if app.openapi_schema is not None:
        msg = "install features before generating or caching OpenAPI"
        raise FeatureConfigurationError(msg)
    validation_app = FastAPI()
    validation_app.exception_handlers.update(app.exception_handlers)
    validation_app.router.routes = list(app.router.routes)
    for router in routers:
        validation_app.include_router(router)
    for spec in handler_specs:
        validation_app.add_exception_handler(spec.exception, spec.handler)
    try:
        registry.install(
            validation_app,
            include_validation_error=include_validation_error,
            include_http_exceptions=include_http_exceptions,
            include_unhandled_error=include_unhandled_error,
        )
    except FaultConfigurationError as error:
        raise FeatureConfigurationError(str(error)) from error


def _compose_lifespan(
    original: FeatureLifespan,
    feature_lifespans: tuple[FeatureLifespan, ...],
    container: AsyncContainer | None,
) -> FeatureLifespan:
    @asynccontextmanager
    async def composed(app: FastAPI) -> AsyncIterator[Mapping[str, Any]]:
        async with AsyncExitStack() as stack:
            states: list[Mapping[str, object]] = []
            original_state = await stack.enter_async_context(original(app))
            if original_state is not None:
                states.append(original_state)
            if container is not None:
                stack.push_async_callback(container.close)
            for lifespan in feature_lifespans:
                state = await stack.enter_async_context(lifespan(app))
                if state is not None:
                    states.append(state)
            merged_state = {
                key: value for state in states for key, value in state.items()
            }
            yield merged_state

    return composed
