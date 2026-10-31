import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from threading import Event
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketState
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcChannel,
    RpcConnection,
    RpcDisconnect,
    RpcErrorBinding,
    RpcReject,
    RpcRejection,
    RpcRejections,
    RpcService,
    RpcSocket,
)
from fastapi_canon.integrations.websockets.adapters.fastapi import (
    FastApiSocket,
    RpcRoutes,
    create_router,
    serve_websocket,
)
from pydantic import BaseModel
from starlette.testclient import WebSocketDenialResponse


class EchoParams(BaseModel):
    value: str


def create_service(
    *, path: str = "/rpc", connections: list[RpcConnection] | None = None
) -> RpcService:
    channel = RpcChannel("demo")

    @channel.method()
    async def echo(params: EchoParams, connection: Inject[RpcConnection]) -> EchoParams:
        if connections is not None:
            connections.append(connection)
        return params

    service = RpcService()
    service.socket(path, channels=(channel,))
    return service


def test_router_serves_websocket_with_fastapi_options() -> None:
    dependencies_called = []
    handshakes = []

    async def dependency() -> None:
        dependencies_called.append(True)

    web = FastAPI()
    web.include_router(
        create_router(create_service(path="/rpc/{endpoint}", connections=handshakes)),
        prefix="/api",
        dependencies=[Depends(dependency)],
    )

    with (
        TestClient(web) as client,
        client.websocket_connect(
            "/api/rpc/primary?endpoint=ignored&view=compact",
            headers={"x-test": "yes"},
            subprotocols=["rpc.test"],
        ) as websocket,
    ):
        websocket.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "demo.echo",
                "params": {"value": "hello"},
            }
        )
        assert websocket.receive_json() == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"value": "hello"},
        }

    assert dependencies_called == [True]
    assert len(handshakes) == 1
    connection = handshakes[0]
    assert connection.path == "/api/rpc/primary"
    assert connection.path_params == {"endpoint": "primary"}
    assert connection.query_params == {"endpoint": "ignored", "view": "compact"}
    assert connection.headers["x-test"] == "yes"
    assert connection.subprotocols == ("rpc.test",)
    assert connection.client is not None


def test_repeated_test_client_disconnects_do_not_leak_cancellation() -> None:
    web = FastAPI()
    web.include_router(create_router(create_service()))

    with TestClient(web) as client:
        for value in range(200):
            with client.websocket_connect("/rpc") as websocket:
                websocket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": value,
                        "method": "demo.echo",
                        "params": {"value": str(value)},
                    }
                )
                assert websocket.receive_json()["result"] == {"value": str(value)}


def test_router_serves_binary_stream() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        yield b"frame"

    service = RpcService()
    service.stream("/frames", frames)
    web = FastAPI()
    web.include_router(create_router(service))

    with (
        TestClient(web) as client,
        client.websocket_connect("/frames") as websocket,
    ):
        assert websocket.receive_bytes() == b"frame"


async def test_send_translates_websocket_disconnect() -> None:
    class DisconnectedWebSocket:
        async def send_text(self, message: str) -> None:
            raise WebSocketDisconnect()

    socket = FastApiSocket.__new__(FastApiSocket)
    socket._websocket = DisconnectedWebSocket()

    try:
        await socket.send("message")
    except RpcDisconnect:
        pass
    else:
        raise AssertionError("RpcDisconnect was not raised")


async def test_send_preserves_unexpected_runtime_errors() -> None:
    class BrokenWebSocket:
        async def send_text(self, message: str) -> None:
            raise RuntimeError("invalid WebSocket state")

    socket = FastApiSocket.__new__(FastApiSocket)
    socket._websocket = BrokenWebSocket()

    try:
        await socket.send("message")
    except RuntimeError as error:
        assert str(error) == "invalid WebSocket state"
    else:
        raise AssertionError("RuntimeError was not raised")


def test_router_serves_bidirectional_streams_without_cancellation_leaks() -> None:
    channel = RpcChannel("voice")

    @channel.stream()
    async def media(
        session_id: int,
        frames: Inject[RpcBinaryInput],
        output: Inject[RpcBinaryOutput],
    ) -> None:
        async for frame in frames:
            await output.send(str(session_id).encode() + b":" + frame)

    service = RpcService()
    service.stream("/voice/{session_id}/media", media)
    web = FastAPI()
    web.include_router(create_router(service))

    with TestClient(web) as client:
        for session_id in range(200):
            with client.websocket_connect(f"/voice/{session_id}/media") as websocket:
                websocket.send_bytes(b"pcm")
                assert websocket.receive_bytes() == f"{session_id}:pcm".encode()
                websocket.send_text('{"type":"end"}')
                assert websocket.receive()["code"] == 1000


def test_router_rejects_invalid_stream_path_variables_as_not_found() -> None:
    channel = RpcChannel("voice")

    @channel.stream()
    async def media(session_id: int, output: Inject[RpcBinaryOutput]) -> None: ...

    service = RpcService()
    service.stream("/voice/{session_id}/media", media)
    web = FastAPI()
    web.include_router(create_router(service))

    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect("/voice/abc/media"),
    ):
        pass
    assert denied.value.status_code == 404


@pytest.mark.parametrize(
    ("rejection", "status"),
    [(RpcRejection.UNAUTHORIZED, 401), (RpcRejection.FORBIDDEN, 403)],
)
def test_before_accept_rejects_with_http_status_and_headers(rejection, status) -> None:
    service = create_service()

    async def authenticate(handshake):
        if handshake.headers.get("authorization") != "Bearer secret":
            raise RpcReject(
                rejection,
                "Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    web = FastAPI()
    web.include_router(create_router(service, before_accept=authenticate))
    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect("/rpc"),
    ):
        pass
    assert denied.value.status_code == status
    assert denied.value.headers["www-authenticate"] == "Bearer"


def test_before_accept_supplies_injected_context() -> None:
    class Principal:
        def __init__(self, name: str) -> None:
            self.name = name

    channel = RpcChannel("auth")

    @channel.method()
    async def who(principal: Inject[Principal]) -> str:
        return principal.name

    async def authenticate(handshake):
        return {Principal: Principal("Ada")}

    service = RpcService()
    service.socket("/auth", channels=(channel,), before_accept=authenticate)
    web = FastAPI()
    web.include_router(create_router(service))
    with TestClient(web) as client, client.websocket_connect("/auth") as websocket:
        websocket.send_json({"jsonrpc": "2.0", "id": 1, "method": "auth.who"})
        assert websocket.receive_json()["result"] == "Ada"


def test_serve_websocket_helper_serves_manual_route() -> None:
    service = create_service()
    web = FastAPI()

    @web.websocket("/rpc")
    async def route(websocket: WebSocket) -> None:
        await serve_websocket(service.endpoint("rpc"), websocket)

    with TestClient(web) as client, client.websocket_connect("/rpc") as websocket:
        websocket.send_json(
            {"jsonrpc": "2.0", "id": 1, "method": "demo.echo", "params": {"value": "x"}}
        )
        assert websocket.receive_json()["result"] == {"value": "x"}


@pytest.mark.parametrize("previous_cancellations", [0, 1])
@pytest.mark.parametrize("cancel_source", ["caller", "drain"])
async def test_serve_websocket_helper_preserves_server_cancellation(
    previous_cancellations: int,
    cancel_source: str,
) -> None:
    service = create_service()
    accepted = asyncio.Event()
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    messages: list[dict[str, Any]] = []

    async def receive():
        return await incoming.get()

    async def send(message):
        messages.append(message)
        if message["type"] == "websocket.accept":
            accepted.set()

    websocket = WebSocket(
        {"type": "websocket", "path": "/rpc", "query_string": b"", "headers": []},
        receive,
        send,
    )

    async def serve() -> None:
        if previous_cancellations:
            task = asyncio.current_task()
            assert task is not None
            task.cancel("previous cancellation")
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
        await serve_websocket(service.endpoint("rpc"), websocket)

    serving = asyncio.create_task(serve())
    try:
        await asyncio.wait_for(accepted.wait(), timeout=5)
    finally:
        if cancel_source == "caller":
            serving.cancel("server cancellation")
            with pytest.raises(asyncio.CancelledError, match="server cancellation"):
                await asyncio.wait_for(serving, timeout=5)
        else:
            await service.drain.drain(timeout=0)
            await asyncio.wait_for(serving, timeout=5)

    assert serving.cancelling() == previous_cancellations + (cancel_source == "caller")
    assert [message["type"] for message in messages] == [
        "websocket.accept",
        "websocket.close",
    ]
    assert service.drain.connection_count == 0


def test_import_without_required_fastapi_reports_broken_install() -> None:
    code = """
import importlib.abc
import sys

class BlockFastApi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fastapi" or fullname.startswith("fastapi."):
            raise ModuleNotFoundError("blocked fastapi", name=fullname)
        return None

sys.meta_path.insert(0, BlockFastApi())
try:
    import fastapi_canon.integrations.websockets.adapters.fastapi
except ModuleNotFoundError as error:
    print(error.name, error, sep="|")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.stdout.strip() == ("fastapi|blocked fastapi")


class Job:
    def __init__(self, job_id: UUID) -> None:
        self.id = job_id


class JobNotFound(Exception):
    pass


job_not_found = RpcErrorBinding(
    JobNotFound, message=str, rejection=RpcRejection.NOT_FOUND
)


class JobOutput:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = frames


class Actor:
    def __init__(self, name: str) -> None:
        self.name = name


@dataclass(frozen=True)
class JobSession:
    job: Job
    actor: Actor


KNOWN_JOB = UUID(int=1)


async def get_actor() -> Actor:
    return Actor("ada")


async def open_job_session(
    job_id: UUID, actor: Annotated[Actor, Depends(get_actor)]
) -> JobSession:
    if job_id != KNOWN_JOB:
        raise JobNotFound("Job not found")
    return JobSession(Job(job_id), actor)


def create_job_service(
    released: list[bool] | None = None,
    *,
    rejects: tuple[RpcErrorBinding[Any], ...] = (),
) -> RpcService:
    events = RpcChannel("jobs")

    @events.method()
    async def describe(session: Inject[JobSession]) -> str:
        return f"{session.actor.name}:{session.job.id}"

    output = RpcChannel("output")

    @output.stream()
    async def frames(
        session: Inject[JobSession], source: Inject[JobOutput]
    ) -> AsyncIterator[bytes]:
        try:
            for frame in source.frames:
                yield frame
            await asyncio.Event().wait()
        finally:
            if released is not None:
                released.append(True)

    failing = RpcChannel("failing")

    @failing.stream()
    async def broken(session: Inject[JobSession]) -> AsyncIterator[bytes]:
        yield b"first"
        raise JobNotFound("Job was deleted")

    service = RpcService(rejects=rejects)
    service.socket(
        "/jobs/{job_id}/events", channels=(events,), name="events", context=JobSession
    )
    service.stream("/jobs/{job_id}/output", frames, name="output", context=JobSession)
    service.stream("/jobs/{job_id}/broken", broken, name="broken", context=JobSession)
    return service


def create_job_app(
    service: RpcService,
    *,
    rejects: tuple[RpcErrorBinding[Any], ...] = (),
    rejections: RpcRejections | None = None,
) -> FastAPI:
    async def resolve(dependency: type) -> JobOutput:
        return JobOutput([b"one", b"two"])

    router = APIRouter(prefix="/jobs")
    routes = RpcRoutes(
        router,
        context=open_job_session,
        resolver=resolve,
        rejects=rejects,
        rejections=rejections,
    )
    for name in ("events", "output", "broken"):
        routes.mount(service.endpoint(name))
    web = FastAPI()
    web.include_router(router)
    return web


def test_rpc_routes_supply_the_context_to_json_rpc_handlers() -> None:
    web = create_job_app(create_job_service())

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket,
    ):
        websocket.send_json({"jsonrpc": "2.0", "id": 1, "method": "jobs.describe"})
        assert websocket.receive_json()["result"] == f"ada:{KNOWN_JOB}"


def test_rpc_routes_serve_binary_streams_and_release_them_on_disconnect() -> None:
    released: list[bool] = []
    web = create_job_app(create_job_service(released))

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/output") as websocket,
    ):
        assert websocket.receive_bytes() == b"one"
        assert websocket.receive_bytes() == b"two"
    assert released == [True]


@pytest.mark.parametrize(
    "rejections",
    [
        {JobNotFound: RpcRejection.NOT_FOUND},
        lambda error: RpcReject(RpcRejection.NOT_FOUND, "Job not found"),
    ],
)
def test_rpc_routes_reject_context_failures_before_accepting(rejections) -> None:
    web = create_job_app(create_job_service(), rejections=rejections)

    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/jobs/{UUID(int=2)}/events"),
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.text == "Job not found"


def test_rpc_routes_reject_context_failures_their_contracts_cover() -> None:
    web = create_job_app(create_job_service(), rejects=(job_not_found,))

    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/jobs/{UUID(int=2)}/events"),
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.text == "Job not found"


def test_rpc_routes_raises_needs_contracts_with_a_rejection() -> None:
    with pytest.raises(ProtocolDefinitionError, match="with a rejection"):
        RpcRoutes(APIRouter(), rejects=(RpcErrorBinding(JobNotFound),))


def test_rpc_routes_reject_raised_rejections_with_their_headers() -> None:
    async def authenticate(job_id: UUID) -> JobSession:
        raise RpcReject(
            RpcRejection.UNAUTHORIZED,
            "Sign in",
            headers={"WWW-Authenticate": "Bearer"},
        )

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=authenticate).mount(
        create_job_service().endpoint("events")
    )
    web = FastAPI()
    web.include_router(router)

    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/events"),
    ):
        pass
    assert denied.value.status_code == 401
    assert denied.value.headers["www-authenticate"] == "Bearer"


def test_rpc_routes_preserve_unmapped_context_failures() -> None:
    web = create_job_app(create_job_service())

    with (
        TestClient(web) as client,
        pytest.raises(JobNotFound),
        client.websocket_connect(f"/jobs/{UUID(int=2)}/events"),
    ):
        pass


def test_rpc_routes_close_accepted_streams_with_mapped_rejections() -> None:
    web = create_job_app(
        create_job_service(), rejections={JobNotFound: RpcRejection.NOT_FOUND}
    )

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/broken") as websocket,
    ):
        assert websocket.receive_bytes() == b"first"
        assert websocket.receive() == {
            "type": "websocket.close",
            "code": 1008,
            "reason": "Job was deleted",
        }


def test_rpc_routes_fall_back_to_the_service_contracts() -> None:
    web = create_job_app(
        create_job_service(rejects=(job_not_found,)),
        rejections={KeyError: RpcRejection.UNAUTHORIZED},
    )

    with TestClient(web) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            client.websocket_connect(f"/jobs/{UUID(int=2)}/events"),
        ):
            pass
        with client.websocket_connect(f"/jobs/{KNOWN_JOB}/broken") as websocket:
            assert websocket.receive_bytes() == b"first"
            assert websocket.receive()["code"] == 1008
    assert denied.value.status_code == 404


def test_rpc_routes_key_a_subclass_context_by_the_declared_type() -> None:
    class GuestJobSession(JobSession):
        pass

    async def open_guest_session(job_id: UUID) -> GuestJobSession:
        return GuestJobSession(Job(job_id), Actor("guest"))

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_guest_session).mount(
        create_job_service().endpoint("events")
    )
    web = FastAPI()
    web.include_router(router)

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket,
    ):
        websocket.send_json({"jsonrpc": "2.0", "id": 1, "method": "jobs.describe"})
        assert websocket.receive_json()["result"] == f"guest:{KNOWN_JOB}"


def test_rpc_routes_without_context_mount_endpoints_without_context() -> None:
    router = APIRouter()
    RpcRoutes(router).mount(create_service().endpoint("rpc"))
    web = FastAPI()
    web.include_router(router)

    with TestClient(web) as client, client.websocket_connect("/rpc") as websocket:
        websocket.send_json(
            {"jsonrpc": "2.0", "id": 1, "method": "demo.echo", "params": {"value": "x"}}
        )
        assert websocket.receive_json()["result"] == {"value": "x"}


def test_rpc_routes_use_a_fastapi_resolver_per_connection() -> None:
    resolved: list[WebSocket] = []
    prepared: list[str] = []

    class Resolver:
        def for_websocket(self, websocket: WebSocket):
            async def resolve(dependency: type) -> JobOutput:
                resolved.append(websocket)
                return JobOutput([b"resolved"])

            return resolve

        def dependency(self, function):
            prepared.append(function.__name__)
            return function

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, resolver=Resolver()).mount(
        create_job_service().endpoint("output")
    )
    web = FastAPI()
    web.include_router(router)

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/output") as websocket,
    ):
        assert websocket.receive_bytes() == b"resolved"
    assert len(resolved) == 1
    assert prepared == ["open_job_session"]


async def untyped_session(job_id: UUID):
    return None


async def any_session(job_id: UUID) -> Any:
    return None


async def open_actor(job_id: UUID) -> Actor:
    return Actor("ada")


def test_rpc_routes_reject_invalid_mounts() -> None:
    service = create_job_service()
    routes = RpcRoutes(APIRouter(prefix="/job"), context=open_job_session)

    with pytest.raises(ValueError, match="outside the router prefix"):
        routes.mount(service.endpoint("events"))
    for context in (untyped_session, any_session):
        with pytest.raises(TypeError, match="needs a concrete class"):
            RpcRoutes(APIRouter(), context=context)
    with pytest.raises(TypeError, match="expects an async function"):
        RpcRoutes(APIRouter(), context=JobSession)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="declares context none, but these routes"):
        RpcRoutes(APIRouter(), context=open_actor).mount(
            create_service().endpoint("rpc")  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="declares context JobSession, but these"):
        RpcRoutes(APIRouter(prefix="/jobs")).mount(service.endpoint("events"))
    with pytest.raises(TypeError, match="JobSession, but these routes provide Actor"):
        RpcRoutes(APIRouter(prefix="/jobs"), context=open_actor).mount(
            service.endpoint("events")
        )

    routes = RpcRoutes(APIRouter(prefix="/jobs"), context=open_job_session)
    routes.mount(service.endpoint("events"))
    with pytest.raises(ValueError, match="already mounted"):
        routes.mount(service.endpoint("events"))


@pytest.mark.parametrize("endpoint_name", ["events", "output"])
def test_custom_handlers_receive_context_and_dependencies_at_the_declared_route(
    endpoint_name: str,
) -> None:
    service = create_job_service()
    document = service.contract(title="Jobs", base_url="ws://example.com").to_openrpc()
    resolved: list[type] = []
    calls: list[JobSession] = []

    async def resolve(dependency: type) -> JobOutput:
        resolved.append(dependency)
        return JobOutput([b"custom"])

    async def label() -> str:
        return "fastapi"

    async def serve_job(
        socket: WebSocket,
        job: Inject[JobSession],
        output: Inject[JobOutput],
        job_id: UUID,
        label: Annotated[str, Depends(label)],
    ) -> None:
        assert socket.application_state is WebSocketState.CONNECTING
        assert job.job.id == job_id
        calls.append(job)
        await socket.accept()
        await socket.send_json({"actor": job.actor.name, "label": label})
        await socket.send_bytes(output.frames[0])
        await socket.close()

    router = APIRouter(prefix="/jobs")
    endpoint = service.endpoint(endpoint_name)
    routes = RpcRoutes(router, context=open_job_session, resolver=resolve)
    routes.mount(endpoint, handler=serve_job)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    assert app.url_path_for(endpoint_name, job_id=str(KNOWN_JOB)) == (
        f"/api/jobs/{KNOWN_JOB}/{endpoint_name}"
    )
    assert service.contract(title="Jobs", base_url="ws://example.com").to_openrpc() == (
        document
    )
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/api/jobs/{KNOWN_JOB}/{endpoint_name}") as socket,
    ):
        assert socket.receive_json() == {"actor": "ada", "label": "fastapi"}
        assert socket.receive_bytes() == b"custom"
        assert socket.receive()["code"] == 1000
    assert resolved == [JobOutput]
    assert len(calls) == 1
    with pytest.raises(ValueError, match="already mounted"):
        routes.mount(endpoint, handler=serve_job)


@pytest.mark.parametrize(
    "stage", ["dependency", "handler_dependency", "injection", "handler", "cleanup"]
)
def test_custom_handlers_reject_failures_before_acceptance(stage: str) -> None:
    calls: list[str] = []

    async def authorize() -> None:
        if stage == "dependency":
            raise JobNotFound("Unavailable")

    async def context(
        job_id: UUID, authorized: Annotated[None, Depends(authorize)]
    ) -> JobSession:
        return JobSession(Job(job_id), Actor("ada"))

    async def handler_dependency() -> None:
        if stage == "handler_dependency":
            raise JobNotFound("Unavailable")

    class Resolver:
        async def resolve(self, dependency: type) -> JobOutput:
            if stage == "injection":
                raise JobNotFound("Unavailable")
            return JobOutput([])

        @asynccontextmanager
        async def enter_connection(self, values) -> AsyncGenerator["Resolver", None]:
            calls.append("enter")
            try:
                yield self
            finally:
                calls.append("exit")
                if stage == "cleanup":
                    raise JobNotFound("Unavailable")

    async def handler(
        socket: WebSocket,
        output: Inject[JobOutput],
        authorized: Annotated[None, Depends(handler_dependency)],
    ) -> None:
        calls.append("handler")
        if stage == "handler":
            raise JobNotFound("Unavailable")

    router = APIRouter(prefix="/jobs")
    routes = RpcRoutes(router, context=context, resolver=Resolver())
    routes.mount(
        create_job_service(rejects=(job_not_found,)).endpoint("events"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/events"),
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.text == "Unavailable"
    assert (
        calls
        == {
            "dependency": [],
            "handler_dependency": [],
            "injection": ["enter", "exit"],
            "handler": ["enter", "handler", "exit"],
            "cleanup": ["enter", "handler", "exit"],
        }[stage]
    )


@pytest.mark.parametrize("policy", ["service", "endpoint", "routes"])
def test_custom_handlers_use_rejection_precedence_after_acceptance(policy: str) -> None:
    service_binding = RpcErrorBinding(
        JobNotFound, message="service", rejection=RpcRejection.NOT_FOUND
    )
    endpoint_binding = RpcErrorBinding(
        JobNotFound, message="endpoint", rejection=RpcRejection.UNAVAILABLE
    )
    route_binding = RpcErrorBinding(
        JobNotFound, message="routes", rejection=RpcRejection.FORBIDDEN
    )
    channel = RpcChannel("jobs")
    service = RpcService(rejects=(service_binding,))
    endpoint = service.socket(
        "/jobs/{job_id}/events",
        channels=(channel,),
        context=JobSession,
        rejects=() if policy == "service" else (endpoint_binding,),
    )

    async def handler(websocket: WebSocket, job: Inject[JobSession]) -> None:
        await websocket.accept()
        await websocket.send_text("pending")
        raise JobNotFound()

    router = APIRouter(prefix="/jobs")
    routes = RpcRoutes(
        router,
        context=open_job_session,
        rejects=(route_binding,) if policy == "routes" else (),
    )
    routes.mount(endpoint, handler=handler)
    app = FastAPI()
    app.include_router(router)
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket,
    ):
        assert websocket.receive_text() == "pending"
        assert websocket.receive() == {
            "type": "websocket.close",
            "code": 1013 if policy == "endpoint" else 1008,
            "reason": policy,
        }


@pytest.mark.parametrize("closed", [False, True])
def test_custom_handlers_preserve_unknown_failures_and_do_not_close_twice(
    closed: bool,
) -> None:
    async def handler(websocket: WebSocket) -> None:
        await websocket.accept()
        if closed:
            await websocket.close(1000, "done")
            raise JobNotFound("already closed")
        raise RuntimeError("unknown")

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, rejects=(job_not_found,)).mount(
        create_job_service().endpoint("events"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        if closed:
            with client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket:
                assert websocket.receive()["reason"] == "done"
        else:
            with (
                pytest.raises(RuntimeError, match="unknown"),
                client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket,
            ):
                websocket.receive()


def test_custom_handlers_cancel_pending_work_and_release_the_connection_scope() -> None:
    calls: list[str] = []
    released = Event()

    class Resolver:
        async def resolve(self, dependency: type) -> JobOutput:
            return JobOutput([])

        @asynccontextmanager
        async def enter_connection(self, values) -> AsyncGenerator["Resolver", None]:
            calls.append("enter")
            assert values[JobSession].job.id == KNOWN_JOB
            try:
                yield self
            finally:
                calls.append("exit")
                released.set()

    async def handler(websocket: WebSocket, job: Inject[JobSession]) -> None:
        acquiring = asyncio.Event()

        async def acquire() -> None:
            calls.append("acquire")
            acquiring.set()
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("cancelled")

        await websocket.accept()
        pending = asyncio.create_task(acquire())
        try:
            await acquiring.wait()
            await websocket.send_text("pending")
            await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, resolver=Resolver()).mount(
        create_job_service().endpoint("events"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        with client.websocket_connect(f"/jobs/{KNOWN_JOB}/events") as websocket:
            assert websocket.receive_text() == "pending"
        assert released.wait(timeout=5)
        assert calls == ["enter", "acquire", "cancelled", "exit"]


def test_custom_handlers_authorize_even_without_context_parameters() -> None:
    called: list[bool] = []

    async def handler() -> None:
        called.append(True)
        raise RpcReject(RpcRejection.FORBIDDEN, "not accepted")

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, rejects=(job_not_found,)).mount(
        create_job_service().endpoint("events"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for job_id, code in [(KNOWN_JOB, 403), (UUID(int=2), 404)]:
            with (
                pytest.raises(WebSocketDenialResponse) as denied,
                client.websocket_connect(f"/jobs/{job_id}/events"),
            ):
                pass
            assert denied.value.status_code == code
    assert called == [True]


def test_custom_handlers_require_explicit_context_injection() -> None:
    routes = RpcRoutes(APIRouter(prefix="/jobs"), context=open_job_session)
    endpoint = create_job_service().endpoint("events")

    async def unmarked(websocket: WebSocket, task: JobSession) -> None: ...

    class OtherSession(JobSession):
        pass

    async def incompatible(task: Inject[OtherSession]) -> None: ...

    def sync(websocket: WebSocket) -> None: ...

    async def variadic(**arguments: Any) -> None: ...

    for handler, message in [
        (unmarked, r"must use Inject\[JobSession\]"),
        (incompatible, "must inject the declared context type JobSession"),
        (sync, "expects an async function"),
        (variadic, "must be positional-or-keyword or keyword-only"),
    ]:
        with pytest.raises(TypeError, match=message):
            routes.mount(endpoint, handler=handler)  # type: ignore[arg-type]


async def test_custom_handlers_release_the_scope_on_cancellation() -> None:
    calls: list[str] = []
    started = asyncio.Event()

    class Resolver:
        @asynccontextmanager
        async def enter_connection(self, values) -> AsyncGenerator["Resolver", None]:
            calls.append("enter")
            try:
                yield self
            finally:
                calls.append("exit")

        async def resolve(self, dependency: type) -> JobOutput:
            return JobOutput([])

    async def handler(websocket: WebSocket, job: Inject[JobSession]) -> None:
        await websocket.accept()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("handler cleanup")

    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, resolver=Resolver()).mount(
        create_job_service().endpoint("events"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        pass

    serving = asyncio.create_task(
        app(
            {
                "type": "websocket",
                "path": f"/jobs/{KNOWN_JOB}/events",
                "query_string": b"",
                "headers": [],
            },
            receive,
            send,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
    assert calls == ["enter", "handler cleanup", "exit"]


def test_custom_handlers_without_context_preserve_internal_parameter_names() -> None:
    output = JobOutput([b"custom"])

    async def resolve(dependency: type) -> JobOutput:
        assert dependency is JobOutput
        return output

    async def handler(
        *,
        _canon_websockets_websocket: Inject[JobOutput],
        _canon_websockets_context: str = "custom",
    ) -> None:
        assert _canon_websockets_websocket is output
        assert _canon_websockets_context == "custom"
        raise RpcReject(RpcRejection.FORBIDDEN, "custom rejection")

    router = APIRouter()
    RpcRoutes(router, resolver=resolve).mount(
        create_service().endpoint("rpc"), handler=handler
    )
    app = FastAPI()
    app.include_router(router)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect("/rpc"),
    ):
        pass
    assert denied.value.status_code == 403
    assert denied.value.text == "custom rejection"


class JobDeleted(Exception):
    pass


def create_socket_app(released: list[bool]) -> FastAPI:
    shell = RpcChannel("jobs")

    @shell.socket(
        closes=(
            RpcErrorBinding(
                JobDeleted, rejection=RpcRejection.NOT_FOUND, message="Job deleted"
            ),
        )
    )
    async def terminal(socket: Inject[RpcSocket], session: Inject[JobSession]) -> None:
        try:
            await socket.send(f"hello {session.actor.name}")
            async for message in socket:
                if message == "delete":
                    raise JobDeleted
                await socket.send(message)
        finally:
            released.append(True)

    service = RpcService()
    service.socket(
        "/jobs/{job_id}/terminal", terminal, name="terminal", context=JobSession
    )
    router = APIRouter(prefix="/jobs")
    RpcRoutes(router, context=open_job_session, rejects=(job_not_found,)).mount(
        service.endpoint("terminal")
    )
    web = FastAPI()
    web.include_router(router)
    return web


def test_rpc_routes_serve_sockets_with_mixed_messages() -> None:
    released: list[bool] = []
    web = create_socket_app(released)

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/terminal") as websocket,
    ):
        assert websocket.receive_text() == "hello ada"
        websocket.send_bytes(b"\x00\xff")
        assert websocket.receive_bytes() == b"\x00\xff"
        websocket.send_text('{"id":1}')
        assert websocket.receive_text() == '{"id":1}'
        websocket.send_text("delete")
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
    assert (closed.value.code, closed.value.reason) == (1008, "Job deleted")
    assert released == [True]


def test_rpc_routes_reject_sockets_before_accepting() -> None:
    released: list[bool] = []
    web = create_socket_app(released)

    with (
        TestClient(web) as client,
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/jobs/{UUID(int=2)}/terminal"),
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.text == "Job not found"
    assert released == []


def test_socket_disconnects_release_the_handler() -> None:
    released: list[bool] = []
    web = create_socket_app(released)

    with (
        TestClient(web) as client,
        client.websocket_connect(f"/jobs/{KNOWN_JOB}/terminal") as websocket,
    ):
        assert websocket.receive_text() == "hello ada"
    assert released == [True]
