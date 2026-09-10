from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from dishka import AsyncContainer, Provider, make_async_container
from dishka.exceptions import DishkaError  # type: ignore[attr-defined]  # not exported
from dishka.integrations.fastapi import setup_dishka
from fastapi import APIRouter, FastAPI
from starlette.types import ExceptionHandler, Lifespan

from fastapi_canon.error import ErrorConfigurationError, ErrorRegistry
from fastapi_canon.openapi import install_openapi_contracts
from fastapi_canon.router import CanonRouter

type FeatureLifespan = Lifespan[FastAPI]
type ProviderFactory = Callable[[], Provider]
type ProviderSource = Provider | ProviderFactory
type RouterFactory = Callable[[], APIRouter]

_INSTALLATION_STATE_KEY = "_fastapi_canon_installation"


class FeatureConfigurationError(ErrorConfigurationError):
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

    name: str
    routers: tuple[APIRouter, ...]
    providers: tuple[ProviderSource, ...]
    errors: ErrorRegistry | None
    exception_handlers: tuple[ExceptionHandlerSpec, ...]
    lifespan: FeatureLifespan | None

    def __init__(
        self,
        *,
        name: str,
        routers: Sequence[APIRouter] = (),
        providers: Sequence[ProviderSource] = (),
        errors: ErrorRegistry | None = None,
        exception_handlers: Sequence[ExceptionHandlerSpec] = (),
        lifespan: FeatureLifespan | None = None,
    ) -> None:
        normalized_name = _validate_feature_name(name)
        normalized_routers = _unique_instances(
            routers, APIRouter, f"feature {normalized_name!r} routers"
        )
        normalized_providers = _unique_provider_sources(
            providers, feature_name=normalized_name
        )
        normalized_handlers = _unique_instances(
            exception_handlers,
            ExceptionHandlerSpec,
            f"feature {normalized_name!r} exception_handlers",
        )
        raw_errors: object = errors
        raw_lifespan: object = lifespan
        if raw_errors is not None and not isinstance(raw_errors, ErrorRegistry):
            msg = f"feature {normalized_name!r} errors must be an ErrorRegistry instance or None"
            raise FeatureConfigurationError(msg)
        if raw_lifespan is not None and not callable(raw_lifespan):
            msg = f"feature {normalized_name!r} lifespan must be callable or None"
            raise FeatureConfigurationError(msg)

        resolved_errors = errors
        if resolved_errors is None:
            resolved_errors = _collect_router_errors(
                normalized_routers,
                feature_name=normalized_name,
            )

        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "routers", normalized_routers)
        object.__setattr__(self, "providers", normalized_providers)
        object.__setattr__(self, "errors", resolved_errors)
        object.__setattr__(self, "exception_handlers", normalized_handlers)
        object.__setattr__(self, "lifespan", lifespan)


@dataclass(frozen=True, slots=True, init=False)
class ErrorOptions:
    """Application-wide options for composing feature error registries."""

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
    errors: ErrorOptions
    router_factory: RouterFactory | None

    def __init__(
        self,
        *features: Feature,
        errors: ErrorOptions | None = None,
        router_factory: RouterFactory | None = None,
    ) -> None:
        normalized_features = _unique_instances(features, Feature, "features")
        _validate_unique_feature_names(normalized_features)
        raw_errors: object = errors
        raw_router_factory: object = router_factory
        if raw_errors is not None and not isinstance(raw_errors, ErrorOptions):
            msg = "errors must be an ErrorOptions instance or None"
            raise FeatureConfigurationError(msg)
        if raw_router_factory is not None and not callable(raw_router_factory):
            msg = "router_factory must be callable or None"
            raise FeatureConfigurationError(msg)
        object.__setattr__(self, "features", normalized_features)
        object.__setattr__(self, "errors", errors or ErrorOptions())
        object.__setattr__(self, "router_factory", router_factory)

    def apply(self, app: FastAPI) -> FastAPI:
        """Apply this composition to *app* exactly once and return the app."""
        _apply_composition(app, self)
        return app


@dataclass(frozen=True, slots=True)
class _Installation:
    features: tuple[Feature, ...] = field(compare=False)
    feature_ids: tuple[int, ...]
    errors: ErrorOptions
    router_factory: RouterFactory | None = field(compare=False)
    router_factory_id: int | None
    container: AsyncContainer | None = field(compare=False)


@dataclass(frozen=True, slots=True)
class _MaterializedProvider:
    feature_name: str
    provider: Provider


def _apply_composition(app: FastAPI, composition: Composition) -> None:
    raw_app: object = app
    if not isinstance(raw_app, FastAPI):
        msg = "app must be a FastAPI instance"
        raise FeatureConfigurationError(msg)
    requested = _Installation(
        features=composition.features,
        feature_ids=tuple(id(feature) for feature in composition.features),
        errors=composition.errors,
        router_factory=composition.router_factory,
        router_factory_id=(
            id(composition.router_factory)
            if composition.router_factory is not None
            else None
        ),
        container=None,
    )
    installed = getattr(app.state, _INSTALLATION_STATE_KEY, None)
    if installed is not None:
        if not isinstance(installed, _Installation) or installed != requested:
            msg = "a different composition is already applied to this application"
            raise FeatureConfigurationError(msg)
        return

    routers = _flatten_routers(composition.features)
    provider_sources = _flatten_provider_sources(composition.features)
    handler_specs = _flatten_handlers(composition.features)
    lifespans = tuple(
        feature.lifespan
        for feature in composition.features
        if feature.lifespan is not None
    )
    _reject_duplicate_contributions(composition.features, "routers", "router")
    _reject_duplicate_contributions(
        composition.features, "providers", "provider source"
    )
    materialized_providers = _materialize_providers(provider_sources)
    _reject_duplicate_materialized_providers(materialized_providers)
    _validate_handlers(app, composition.features)
    installed_routers = _prepare_routers(routers, composition.router_factory)

    registries = tuple(
        (feature.name, feature.errors)
        for feature in composition.features
        if feature.errors is not None
    )
    registry = _merge_errors(
        registries,
        name=composition.errors.registry_name,
        type_base=composition.errors.type_base,
    )
    container = _make_container(
        tuple(contribution.provider for contribution in materialized_providers)
    )
    _validate_error_installation(
        app,
        installed_routers,
        handler_specs,
        registry,
        include_validation_error=composition.errors.include_validation_error,
        include_http_exceptions=composition.errors.include_http_exceptions,
        include_unhandled_error=composition.errors.include_unhandled_error,
    )
    if container is not None and app.middleware_stack is not None:
        msg = "features with providers must be installed before the app starts"
        raise FeatureConfigurationError(msg)

    original_lifespan = app.router.lifespan_context
    for router in installed_routers:
        app.include_router(router)
    for spec in handler_specs:
        app.add_exception_handler(spec.exception, spec.handler)
    if registry is not None:
        registry.install(
            app,
            include_validation_error=composition.errors.include_validation_error,
            include_http_exceptions=composition.errors.include_http_exceptions,
            include_unhandled_error=composition.errors.include_unhandled_error,
        )
    else:
        install_openapi_contracts(app)
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
        errors=composition.errors,
        router_factory=composition.router_factory,
        router_factory_id=requested.router_factory_id,
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


def _collect_router_errors(
    routers: tuple[APIRouter, ...], *, feature_name: str
) -> ErrorRegistry | None:
    registries: list[tuple[str, ErrorRegistry]] = []
    seen: set[int] = set()
    for router in routers:
        if not isinstance(router, CanonRouter) or router.error_registry is None:
            continue
        if id(router.error_registry) in seen:
            continue
        seen.add(id(router.error_registry))
        registries.append((feature_name, router.error_registry))
    return _merge_errors(
        tuple(registries),
        name=f"{feature_name}-routers" if len(registries) > 1 else None,
        type_base=None,
    )


def _validate_optional_string(value: object, parameter: str) -> None:
    if value is not None and not isinstance(value, str):
        msg = f"{parameter} must be a string or None"
        raise FeatureConfigurationError(msg)


def _validate_feature_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        msg = "feature name must be a non-empty, single-line string"
        raise FeatureConfigurationError(msg)
    return value


def _validate_unique_feature_names(features: tuple[Feature, ...]) -> None:
    seen: dict[str, int] = {}
    for index, feature in enumerate(features):
        previous = seen.get(feature.name)
        if previous is not None:
            msg = (
                f"feature name {feature.name!r} is duplicated at "
                f"features[{previous}] and features[{index}]"
            )
            raise FeatureConfigurationError(msg)
        seen[feature.name] = index


def _validate_bool(value: object, parameter: str) -> None:
    if not isinstance(value, bool):
        msg = f"{parameter} must be a bool"
        raise FeatureConfigurationError(msg)


def _flatten_routers(features: tuple[Feature, ...]) -> tuple[APIRouter, ...]:
    return tuple(router for feature in features for router in feature.routers)


def _unique_provider_sources(
    values: Sequence[ProviderSource], *, feature_name: str
) -> tuple[ProviderSource, ...]:
    result: list[ProviderSource] = []
    seen: set[int] = set()
    for index, value in enumerate(values):
        raw_value: object = value
        if not isinstance(raw_value, Provider) and not callable(raw_value):
            msg = (
                f"feature {feature_name!r} providers[{index}] must be a Provider "
                "instance, Provider class, or zero-argument provider factory"
            )
            raise FeatureConfigurationError(msg)
        if id(value) in seen:
            msg = (
                f"feature {feature_name!r} providers[{index}] duplicates an "
                "earlier provider source"
            )
            raise FeatureConfigurationError(msg)
        seen.add(id(value))
        result.append(value)
    return tuple(result)


def _flatten_provider_sources(
    features: tuple[Feature, ...],
) -> tuple[tuple[str, ProviderSource], ...]:
    return tuple(
        (feature.name, provider)
        for feature in features
        for provider in feature.providers
    )


def _flatten_handlers(
    features: tuple[Feature, ...],
) -> tuple[ExceptionHandlerSpec, ...]:
    return tuple(
        handler for feature in features for handler in feature.exception_handlers
    )


def _reject_duplicate_contributions(
    features: tuple[Feature, ...], attribute: str, kind: str
) -> None:
    seen: dict[int, str] = {}
    for feature in features:
        values = getattr(feature, attribute)
        for value in values:
            previous = seen.get(id(value))
            if previous is not None:
                msg = (
                    f"features {previous!r} and {feature.name!r} contribute the "
                    f"same {kind} instance"
                )
                raise FeatureConfigurationError(msg)
            seen[id(value)] = feature.name


def _materialize_providers(
    sources: tuple[tuple[str, ProviderSource], ...],
) -> tuple[_MaterializedProvider, ...]:
    result: list[_MaterializedProvider] = []
    for feature_name, source in sources:
        if isinstance(source, Provider):
            provider = source
        else:
            try:
                candidate: object = source()
            except Exception as error:
                msg = f"provider factory from feature {feature_name!r} failed"
                raise FeatureConfigurationError(msg) from error
            if not isinstance(candidate, Provider):
                msg = (
                    f"provider factory from feature {feature_name!r} returned "
                    f"{type(candidate).__name__}, expected Provider"
                )
                raise FeatureConfigurationError(msg)
            provider = candidate
        result.append(_MaterializedProvider(feature_name, provider))
    return tuple(result)


def _reject_duplicate_materialized_providers(
    providers: tuple[_MaterializedProvider, ...],
) -> None:
    seen: dict[int, str] = {}
    for contribution in providers:
        previous = seen.get(id(contribution.provider))
        if previous is not None:
            msg = (
                f"provider sources from features {previous!r} and "
                f"{contribution.feature_name!r} produced the same Provider instance"
            )
            raise FeatureConfigurationError(msg)
        seen[id(contribution.provider)] = contribution.feature_name


def _prepare_routers(
    routers: tuple[APIRouter, ...], router_factory: RouterFactory | None
) -> tuple[APIRouter, ...]:
    if router_factory is None:
        return routers
    try:
        wrapper: object = router_factory()
    except Exception as error:
        msg = "router_factory failed"
        raise FeatureConfigurationError(msg) from error
    if not isinstance(wrapper, APIRouter):
        msg = f"router_factory returned {type(wrapper).__name__}, expected APIRouter"
        raise FeatureConfigurationError(msg)
    if wrapper.routes:
        msg = "router_factory must return a fresh APIRouter without routes"
        raise FeatureConfigurationError(msg)
    try:
        for router in routers:
            wrapper.include_router(router)
    except Exception as error:
        msg = "composition router failed to include feature routers"
        raise FeatureConfigurationError(msg) from error
    return (wrapper,)


def _validate_handlers(app: FastAPI, features: tuple[Feature, ...]) -> None:
    seen: dict[type[Exception], str] = {}
    for feature in features:
        for spec in feature.exception_handlers:
            previous = seen.get(spec.exception)
            if previous is not None:
                msg = (
                    f"features {previous!r} and {feature.name!r} define an "
                    "exception handler for "
                    f"{spec.exception.__qualname__}"
                )
                raise FeatureConfigurationError(msg)
            seen[spec.exception] = feature.name
            if spec.exception in app.exception_handlers:
                msg = (
                    f"feature {feature.name!r} defines an exception handler for "
                    f"{spec.exception.__qualname__}, but the application already "
                    "defines one"
                )
                raise FeatureConfigurationError(msg)


def _merge_errors(
    registries: tuple[tuple[str, ErrorRegistry], ...],
    *,
    name: str | None,
    type_base: str | None,
) -> ErrorRegistry | None:
    if not registries:
        return None
    distinct: list[tuple[str, ErrorRegistry]] = []
    seen: set[int] = set()
    for contribution in registries:
        if id(contribution[1]) in seen:
            continue
        seen.add(id(contribution[1]))
        distinct.append(contribution)
    registries = tuple(distinct)
    inferred_base = type_base
    registry_bases = {registry.type_base for _, registry in registries}
    if inferred_base is None and len(registry_bases) == 1:
        inferred_base = next(iter(registry_bases))
    try:
        return ErrorRegistry.merge(
            *(registry for _, registry in registries),
            name=name,
            type_base=inferred_base,
        )
    except ErrorConfigurationError as error:
        feature_names = ", ".join(repr(feature_name) for feature_name, _ in registries)
        msg = (
            f"error registries from features {feature_names} are incompatible: {error}"
        )
        raise FeatureConfigurationError(msg) from error


def _make_container(providers: tuple[Provider, ...]) -> AsyncContainer | None:
    if not providers:
        return None
    try:
        return make_async_container(*providers)
    except DishkaError as error:
        msg = f"invalid Dishka provider graph: {error}"
        raise FeatureConfigurationError(msg) from error


def _validate_error_installation(
    app: FastAPI,
    routers: tuple[APIRouter, ...],
    handler_specs: tuple[ExceptionHandlerSpec, ...],
    registry: ErrorRegistry | None,
    *,
    include_validation_error: bool,
    include_http_exceptions: bool,
    include_unhandled_error: bool,
) -> None:
    if app.openapi_schema is not None:
        msg = "install features before generating or caching OpenAPI"
        raise FeatureConfigurationError(msg)
    if registry is None:
        return
    validation_app = FastAPI()
    validation_app.exception_handlers.update(app.exception_handlers)
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
    except ErrorConfigurationError as error:
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
