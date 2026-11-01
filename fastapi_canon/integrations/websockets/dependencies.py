import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypeAliasType, cast, get_args, get_origin


class _Inject:
    __slots__ = ()


_INJECT = _Inject()

type Inject[DependencyT] = Annotated[DependencyT, _INJECT]


class RpcResolver(Protocol):
    async def resolve[DependencyT](
        self,
        dependency: type[DependencyT],
    ) -> DependencyT: ...


class RpcResolverScope(Protocol):
    def __call__(
        self,
        resolver: RpcResolver,
    ) -> AbstractAsyncContextManager[RpcResolver]: ...


@dataclass(frozen=True, slots=True)
class RpcInjectedParameter:
    name: str
    dependency: type[Any]


class EmptyResolver:
    async def resolve[DependencyT](self, dependency: type[DependencyT]) -> DependencyT:
        raise LookupError(
            f"No RPC resolver is configured for dependency {dependency!r}"
        )


type RpcResolverLike = RpcResolver | Callable[[type[Any]], Awaitable[Any] | Any]


class FunctionResolver:
    def __init__(self, function: Callable[[type[Any]], Awaitable[Any] | Any]) -> None:
        self._function = function

    async def resolve[DependencyT](self, dependency: type[DependencyT]) -> DependencyT:
        value = self._function(dependency)
        return await value if inspect.isawaitable(value) else value


def as_resolver(value: RpcResolverLike | None) -> RpcResolver:
    if value is None:
        return EmptyResolver()
    if hasattr(value, "resolve"):
        return value  # type: ignore[return-value]
    if callable(value):
        return FunctionResolver(value)
    raise TypeError("resolver must implement resolve() or be callable")


class ContextResolver:
    def __init__(
        self,
        resolver: RpcResolver,
        values: Mapping[type[Any], object],
    ) -> None:
        self._resolver = resolver
        self._values = dict(values)

    @property
    def resolver(self) -> RpcResolver:
        return self._resolver

    @property
    def values(self) -> Mapping[type[Any], object]:
        return self._values

    async def resolve[DependencyT](self, dependency: type[DependencyT]) -> DependencyT:
        if dependency in self._values:
            return self._values[dependency]  # type: ignore[return-value]
        return await self._resolver.resolve(dependency)

    @asynccontextmanager
    async def enter_scope(self) -> AsyncGenerator[RpcResolver, None]:
        async with call_scope(self._resolver) as resolver:
            yield ContextResolver(resolver, self._values)


def resolver_with_context(
    resolver: RpcResolverLike | None,
    context: object | Mapping[type[Any], object] | None,
) -> RpcResolver:
    resolved = as_resolver(resolver)
    values = context_values(context)
    return ContextResolver(resolved, values) if values else resolved


@asynccontextmanager
async def call_scope(resolver: RpcResolver) -> AsyncGenerator[RpcResolver, None]:
    """Enter one resolver scope for an RPC invocation.

    Resolvers with scoped lifetimes may implement ``enter_scope()``. Stateless
    resolvers need only implement ``resolve()`` and are yielded unchanged.
    """
    enter_scope = getattr(resolver, "enter_scope", None)
    if enter_scope is None:
        yield resolver
        return
    async with enter_scope() as scoped:
        yield scoped


@asynccontextmanager
async def connection_scope(
    resolver: RpcResolver,
    context: object | Mapping[type[Any], object] | None = None,
) -> AsyncGenerator[RpcResolver, None]:
    """Enter a connection lifetime and expose its typed context to handlers."""
    values = context_values(context)
    enter_connection = getattr(resolver, "enter_connection", None)
    if enter_connection is None:
        yield ContextResolver(resolver, values) if values else resolver
        return
    async with enter_connection(values) as connection_resolver:
        yield (
            ContextResolver(connection_resolver, values)
            if values
            else connection_resolver
        )


def injected_parameter(
    name: str,
    annotation: Any,
) -> RpcInjectedParameter | None:
    if get_origin(annotation) is Inject:
        dependency = get_args(annotation)[0]
        if not _concrete_dependency(dependency):
            raise TypeError(
                f"Injected RPC parameter {name!r} must name a concrete dependency type"
            )
        return RpcInjectedParameter(name=name, dependency=cast(type[Any], dependency))
    if get_origin(annotation) is not Annotated:
        return None
    dependency, *metadata = get_args(annotation)
    if _INJECT not in metadata:
        return None
    if not _concrete_dependency(dependency):
        raise TypeError(
            f"Injected RPC parameter {name!r} must name a concrete dependency type"
        )
    return RpcInjectedParameter(name=name, dependency=cast(type[Any], dependency))


def _concrete_dependency(dependency: Any) -> bool:
    if inspect.isclass(dependency):
        return True
    from fastapi_canon.integrations.websockets.sockets import RpcSocket

    while isinstance(dependency, TypeAliasType):
        dependency = dependency.__value__
    return get_origin(dependency) is RpcSocket


def context_values(
    context: object | Mapping[type[Any], object] | None,
) -> dict[type[Any], object]:
    if context is None:
        return {}
    if isinstance(context, Mapping):
        return dict(context)
    return {type(context): context}
