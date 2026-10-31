import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from dishka import Provider, Scope, from_context, make_async_container, provide
from dishka.integrations.fastapi import FromDishka, setup_dishka
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcConnection,
    RpcErrorBinding,
    RpcRejection,
    RpcRoutes,
    RpcService,
)
from fastapi_canon.integrations.websockets.adapters.dishka import Dishka, DishkaResolver
from fastapi_canon.integrations.websockets.dependencies import RpcResolver


@dataclass(frozen=True)
class Login:
    user: str
    expires_at: float


class Unauthenticated(Exception):
    pass


class ForbiddenOrigin(Exception):
    pass


class Sessions:
    def __init__(self) -> None:
        # Demo only: replace this store with the application's session service.
        self.values = {"demo": Login("ada", time.time() + 3600)}


@dataclass
class RequestIdentity:
    login: Login


class Dependencies(Provider):
    login = from_context(Login, scope=Scope.SESSION)
    sessions = provide(Sessions, scope=Scope.APP)

    @provide(scope=Scope.REQUEST)
    def identity(self, login: Login) -> RequestIdentity:
        return RequestIdentity(login)


class AuthenticatedResolver(DishkaResolver):
    @asynccontextmanager
    async def enter_connection(
        self, context: Mapping[type[Any], object]
    ) -> AsyncGenerator[RpcResolver, None]:
        login, connection = context[Login], context[RpcConnection]
        assert isinstance(login, Login) and isinstance(connection, RpcConnection)
        connection.expire_at(login.expires_at)
        async with super().enter_connection(context) as resolver:
            yield resolver


class AuthenticatedDishka(Dishka):
    def for_websocket(self, websocket: WebSocket) -> AuthenticatedResolver:
        return AuthenticatedResolver(websocket.app.state.dishka_container)


async def open_session(websocket: WebSocket, sessions: FromDishka[Sessions]) -> Login:
    if websocket.headers.get("origin") != "https://app.example.test":
        raise ForbiddenOrigin("Origin is not allowed")
    login = sessions.values.get(websocket.cookies.get("session", ""))
    if login is None or login.expires_at <= time.time():
        raise Unauthenticated("Session is missing or expired")
    return login


def create_app() -> FastAPI:
    container = make_async_container(Dependencies())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        await container.close()

    app = FastAPI(lifespan=lifespan)
    channel = RpcChannel("account")

    @channel.method()
    async def whoami(identity: Inject[RequestIdentity]) -> str:
        return identity.login.user

    service = RpcService()
    endpoint = service.socket("/rpc", channels=(channel,), context=Login)
    router = APIRouter()
    RpcRoutes(
        router,
        context=open_session,
        resolver=AuthenticatedDishka(),
        rejects=(
            RpcErrorBinding(
                Unauthenticated, message=str, rejection=RpcRejection.UNAUTHORIZED
            ),
            RpcErrorBinding(
                ForbiddenOrigin, message=str, rejection=RpcRejection.FORBIDDEN
            ),
        ),
    ).mount(endpoint)
    app.include_router(router)
    setup_dishka(container, app)
    return app


app = create_app()
