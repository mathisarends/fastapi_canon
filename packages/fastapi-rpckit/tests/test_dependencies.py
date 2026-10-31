from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from fastapi_canon.integrations.websockets import Inject
from fastapi_canon.integrations.websockets.dependencies import (
    _INJECT,
    ContextResolver,
    EmptyResolver,
    RpcInjectedParameter,
    call_scope,
    connection_scope,
    injected_parameter,
)


@dataclass(frozen=True)
class Value:
    name: str


class Resolver:
    def __init__(self, values: Mapping[type[object], object]) -> None:
        self.values = values
        self.calls: list[type[object]] = []

    async def resolve[DependencyT](self, dependency: type[DependencyT]) -> DependencyT:
        self.calls.append(dependency)
        return self.values[dependency]  # type: ignore[return-value]


class ScopedResolver(Resolver):
    def __init__(self, values: Mapping[type[object], object]) -> None:
        super().__init__(values)
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def enter_scope(self) -> AsyncGenerator[Resolver, None]:
        self.entered += 1
        try:
            yield Resolver(self.values)
        finally:
            self.exited += 1


class ConnectionResolver(Resolver):
    def __init__(
        self,
        values: Mapping[type[object], object],
        connection: Resolver,
    ) -> None:
        super().__init__(values)
        self.connection = connection
        self.context: Mapping[type[Any], object] | None = None

    @asynccontextmanager
    async def enter_connection(
        self,
        context: Mapping[type[Any], object],
    ) -> AsyncGenerator[Resolver, None]:
        self.context = context
        yield self.connection


async def test_empty_resolver_explains_that_no_dependency_is_configured() -> None:
    with pytest.raises(LookupError, match="No RPC resolver"):
        await EmptyResolver().resolve(Value)


async def test_context_resolver_prefers_connection_values_and_delegates_others() -> (
    None
):
    direct = Value("connection")
    fallback = Resolver({str: "resolved"})
    resolver = ContextResolver(fallback, {Value: direct})

    assert resolver.resolver is fallback
    assert resolver.values == {Value: direct}
    assert await resolver.resolve(Value) is direct
    assert await resolver.resolve(str) == "resolved"
    assert fallback.calls == [str]


async def test_context_resolver_keeps_values_inside_a_child_call_scope() -> None:
    direct = Value("connection")
    resolver = ScopedResolver({str: "resolved"})
    context = ContextResolver(resolver, {Value: direct})

    async with context.enter_scope() as scoped:
        assert await scoped.resolve(Value) is direct
        assert await scoped.resolve(str) == "resolved"

    assert resolver.entered == resolver.exited == 1


async def test_call_scope_supports_stateless_and_scoped_resolvers() -> None:
    stateless = Resolver({})
    scoped = ScopedResolver({})

    async with call_scope(stateless) as entered:
        assert entered is stateless
    async with call_scope(scoped):
        pass

    assert scoped.entered == scoped.exited == 1


async def test_connection_scope_supports_context_and_connection_hooks() -> None:
    plain = Resolver({})
    direct = Value("direct")

    async with connection_scope(plain) as entered:
        assert entered is plain
    async with connection_scope(plain, direct) as entered:
        assert await entered.resolve(Value) is direct
    async with connection_scope(plain, {Value: direct}) as entered:
        assert await entered.resolve(Value) is direct

    connection = Resolver({str: "connection"})
    hooked = ConnectionResolver({}, connection)
    async with connection_scope(hooked, {Value: direct}) as entered:
        assert await entered.resolve(Value) is direct
        assert await entered.resolve(str) == "connection"

    assert hooked.context == {Value: direct}


def test_injected_parameter_recognizes_only_concrete_injected_types() -> None:
    assert injected_parameter("value", Value) is None
    assert injected_parameter("value", Annotated[Value, "metadata"]) is None
    assert injected_parameter("value", Inject[Value]) == RpcInjectedParameter(
        name="value",
        dependency=Value,
    )
    assert injected_parameter(
        "value", Annotated[Value, _INJECT]
    ) == RpcInjectedParameter(
        name="value",
        dependency=Value,
    )

    with pytest.raises(TypeError, match="concrete dependency type"):
        injected_parameter("values", Inject[list[str]])
    with pytest.raises(TypeError, match="concrete dependency type"):
        injected_parameter("values", Annotated[list[str], _INJECT])
