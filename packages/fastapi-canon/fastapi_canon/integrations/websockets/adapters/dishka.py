from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from inspect import Parameter, iscoroutinefunction, signature
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from dishka import AsyncContainer
    from fastapi import APIRouter, WebSocket

    from fastapi_canon.integrations.websockets.adapters.fastapi import (
        FastApiResolverFactory,
    )
    from fastapi_canon.integrations.websockets.connection import (
        RpcBeforeAccept,
        RpcLimits,
        RpcRejections,
    )
    from fastapi_canon.integrations.websockets.middleware import RpcMiddlewareLike
    from fastapi_canon.integrations.websockets.server import RpcErrorMapper
    from fastapi_canon.integrations.websockets.service import RpcService

from fastapi_canon.integrations.websockets.dependencies import RpcResolver


class DishkaResolver:
    """Adapt a Dishka container to connection and call-scoped RPC resolution."""

    def __init__(self, container: AsyncContainer) -> None:
        self._container = container

    async def resolve[DependencyT](self, dependency: type[DependencyT]) -> DependencyT:
        return await self._container.get(dependency)

    @asynccontextmanager
    async def enter_connection(
        self,
        context: Mapping[type[Any], object],
    ) -> AsyncGenerator[RpcResolver, None]:
        try:
            from dishka import Scope
        except ImportError as error:
            raise ModuleNotFoundError(
                "The Dishka integration requires a complete fastapi-canon installation"
            ) from error
        container_scope = getattr(self._container, "scope", None)
        if container_scope is not None and container_scope is not Scope.APP:
            raise ValueError(
                "DishkaResolver needs the APP container "
                "(app.state.dishka_container), got a "
                f"{container_scope.name} container. Canon opens the SESSION "
                "scope per connection itself."
            )
        async with self._container(
            context=dict(context),
            scope=Scope.SESSION,
        ) as session_container:
            yield DishkaResolver(session_container)

    @asynccontextmanager
    async def enter_scope(self) -> AsyncGenerator[RpcResolver, None]:
        async with self._container() as request_container:
            yield DishkaResolver(request_container)


def dishka_router(
    service: RpcService,
    *,
    context: object | Mapping[type[Any], object] | None = None,
    resolver_factory: FastApiResolverFactory | None = None,
    error_mapper: RpcErrorMapper | None = None,
    limits: RpcLimits | None = None,
    before_accept: RpcBeforeAccept | None = None,
    rejections: RpcRejections | None = None,
    middleware: Sequence[RpcMiddlewareLike] = (),
) -> APIRouter:
    """Mount a service with a context value, not a FastAPI context dependency.

    Use ``RpcRoutes(context=async_dependency, resolver=Dishka(), rejects=...)``
    for per-connection authentication through FastAPI and Dishka dependencies.
    ``resolver_factory`` can customize the resolver built for each WebSocket.
    """
    from fastapi_canon.integrations.websockets import create_router

    if iscoroutinefunction(context):
        raise TypeError(
            "dishka_router context is a value; use RpcRoutes(context=...) "
            "for an async FastAPI context dependency"
        )

    return create_router(
        service,
        resolver_factory=resolver_factory
        or (lambda websocket: DishkaResolver(websocket.app.state.dishka_container)),
        context=context,
        error_mapper=error_mapper,
        limits=limits,
        before_accept=before_accept,
        rejections=rejections,
        middleware=middleware,
    )


class Dishka:
    """Resolve ``RpcRoutes`` dependencies with the app's root Dishka container.

    The context function may declare ``FromDishka[T]`` parameters without
    ``@inject``; they resolve from the container of Dishka's FastAPI
    middleware, so call ``setup_dishka()`` on the app.
    """

    def for_websocket(self, websocket: WebSocket) -> DishkaResolver:
        return DishkaResolver(websocket.app.state.dishka_container)

    def dependency[FunctionT: Callable[..., Any]](
        self, function: FunctionT
    ) -> FunctionT:
        try:
            from dishka.integrations.base import wrap_injection
            from fastapi import WebSocket
        except ImportError as error:
            raise ModuleNotFoundError(
                "Reinstall fastapi-canon with its required Dishka dependency"
            ) from error
        hints = get_type_hints(function)
        parameters = signature(function).parameters
        websocket_name = next(
            (name for name in parameters if hints.get(name) is WebSocket), None
        )
        additional_params = []
        if websocket_name is None:
            websocket_name = "_canon_websockets_websocket"
            while websocket_name in parameters:
                websocket_name = "_" + websocket_name
            additional_params.append(
                Parameter(
                    websocket_name,
                    Parameter.KEYWORD_ONLY,
                    annotation=WebSocket,
                )
            )
        # Dishka's FastAPI injector used Request for missing transport arguments
        # in 1.7; a WebSocket dependency needs the WebSocket's container instead.
        return wrap_injection(
            func=function,
            is_async=True,
            additional_params=additional_params,
            container_getter=lambda _, values: (
                values[websocket_name].state.dishka_container
            ),
        )
