import asyncio
import faulthandler
import importlib
import importlib.util
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.websockets import WebSocketDisconnect

MODULES = {
    "fastapi-canon": "fastapi_canon",
    "sse": "fastapi_canon.integrations.sse",
    "websockets": "fastapi_canon.integrations.websockets",
    "webrtc": "fastapi_canon.integrations.webrtc",
}
STACK = {"fastapi-canon", "sse", "websockets"}


class Event(BaseModel):
    type: Literal["ready"] = "ready"
    value: int = 1


class SocketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class SocketOutput(BaseModel):
    value: int


def main() -> None:
    selected = set(sys.argv[1:])
    assert importlib.util.find_spec("rpckit") is None
    assert importlib.util.find_spec("fastapi_wsrpc") is None
    for legacy in ("fastapi_streams", "fastapi_rpckit", "fastapi_webrtc"):
        assert importlib.util.find_spec(legacy) is None
    for name, module in MODULES.items():
        if name in selected:
            installed = importlib.import_module(module)
            assert Path(installed.__file__).resolve().is_relative_to(Path(sys.prefix))
            if hasattr(installed, "__version__"):
                assert installed.__version__ == version("fastapi-canon")
        else:
            assert importlib.util.find_spec(module) is not None, module
    app = FastAPI()
    if importlib.util.find_spec("jsonschema") and importlib.util.find_spec("jinja2"):
        _offline_sse_codegen()
    routers = []
    if "fastapi-canon" in selected:
        from fastapi_canon import CanonRouter

        http = CanonRouter()

        @http.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        routers.append(http)
        _canon_check()
        _observation_check(enabled="--otel" in selected)
    if "sse" in selected:
        from fastapi_canon.integrations.sse import StreamRouter

        if "fastapi-canon" in selected:
            from fastapi_canon.integrations.sse import CanonStreamRouter

            streams = CanonStreamRouter()
        else:
            streams = StreamRouter()

        @streams.stream("/events", operation_id="events")
        async def events() -> AsyncIterator[Event]:
            yield Event()

        routers.append(streams)
        _cli("sse")
    if "websockets" in selected:
        from fastapi_canon.integrations.websockets import (
            Inject,
            RpcChannel,
            RpcService,
            RpcSocket,
            create_router,
        )

        channel = RpcChannel("health")

        @channel.method("ping")
        async def ping() -> str:
            return "pong"

        rpc = RpcService()
        rpc.socket("/rpc", channels=(channel,))

        @channel.socket("messages")
        async def messages(message: SocketInput) -> SocketOutput:
            return SocketOutput(value=message.value + 1)

        rpc.socket("/messages", messages)

        @channel.socket("session")
        async def session(socket: Inject[RpcSocket[SocketInput, SocketOutput]]) -> None:
            await socket.send(SocketOutput(value=0))
            async for message in socket:
                await socket.send(SocketOutput(value=message.value + 1))

        rpc.socket("/session", session)
        routers.append(create_router(rpc))
        _cli("websockets")
    if "webrtc" in selected:
        engine = _RecordingEngine()
        routers.append(_webrtc_router(engine, canon="fastapi-canon" in selected))
        _cli("webrtc")
    if "fastapi-canon" in selected:
        from fastapi_canon import Composition, ErrorOptions, Feature

        app = Composition(
            Feature(name="installed", routers=routers),
            errors=ErrorOptions(type_base="https://example.test/problems"),
        ).apply(app)
    else:
        for router in routers:
            app.include_router(router)
    with TestClient(app) as client:
        if "fastapi-canon" in selected:
            assert client.get("/health").json() == {"status": "ok"}
        if "sse" in selected:
            assert '"type":"ready"' in client.get("/events").text
        if "websockets" in selected:
            with client.websocket_connect("/rpc") as websocket:
                websocket.send_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "health.ping"}
                )
                assert websocket.receive_json()["result"] == "pong"
            with client.websocket_connect("/messages") as websocket:
                websocket.send_json({"value": 41})
                assert websocket.receive_json() == {"value": 42}
                websocket.send_json({"value": "secret"})
                try:
                    websocket.receive_json()
                except WebSocketDisconnect as error:
                    assert error.code == 1002
                    assert "secret" not in error.reason
                else:
                    raise AssertionError("Typed socket accepted an invalid message")
            with client.websocket_connect("/session") as websocket:
                assert websocket.receive_json() == {"value": 0}
                websocket.send_json({"value": 41})
                assert websocket.receive_json() == {"value": 42}
        if "webrtc" in selected:
            _webrtc_sessions(client, engine, canon="fastapi-canon" in selected)
    assert app.openapi()["openapi"]
    if "fastapi-canon" in selected:
        _router_policies()
    if "webrtc" in selected:
        _webrtc_codegen(app)
    if "websockets" in selected and importlib.util.find_spec("jinja2"):
        _rpc_codegen(rpc)
    if selected >= STACK:
        # Run the same composition/lifecycle regression against installed wheels.

        example = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "examples/stack_service.py")
        )
        stack = example["create_app"]()
        with TestClient(stack) as client:
            assert client.post("/counter").json() == {"value": 1}
            assert '"value":1' in client.get("/counter/events").text
            with client.websocket_connect("/rpc") as websocket:
                websocket.send_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "counter.increment"}
                )
                assert websocket.receive_json()["result"] == {"value": 2}
            counter = client.portal.call(
                stack.state.dishka_container.get, example["Counter"]
            )
        assert counter.closed
    print(f"Installed artifact checks passed: {', '.join(sorted(selected))}")


def _observation_check(*, enabled: bool) -> None:
    """Exercise installed decorators with no optional SDK or source overrides."""
    from dishka import FromDishka, Provider, Scope, provide
    from dishka.integrations.fastapi import inject
    from fastapi_canon import CanonRouter, Composition, Feature
    from fastapi_canon.integrations.opentelemetry import Telemetry, observe

    global_provider = None
    if enabled:
        from opentelemetry import trace

        assert importlib.util.find_spec("opentelemetry.sdk") is None
        global_provider = trace.get_tracer_provider()

    class Port:
        async def ping(self) -> str:
            return "pong"

    class Dependencies(Provider):
        port = provide(Port, scope=Scope.REQUEST)
        port_telemetry = observe(Port)

    router = CanonRouter()

    @router.get("/observed")
    @inject
    async def observed(port: FromDishka[Port]) -> str:
        return await port.ping()

    app = Composition(
        Feature(name="observed", providers=[Dependencies], routers=[router]),
        telemetry=Telemetry() if enabled else None,
    ).apply(FastAPI())
    with TestClient(app) as client:
        assert client.get("/observed").json() == "pong"
    if enabled:
        assert trace.get_tracer_provider() is global_provider
        assert app._telemetry["auto_configure"] is False
    else:
        assert "fastapi_canon.integrations.opentelemetry.ports" not in sys.modules


def _canon_check() -> None:
    with tempfile.TemporaryDirectory(prefix="canon-check-") as temporary:
        root = Path(temporary)
        source = root / "api.py"
        source.write_text(
            "from fastapi_canon import CanonRouter, Error, ErrorRegistry\n"
            "class Missing(Exception): pass\n"
            "missing = Error(Missing, status=404, code='missing', title='Missing')\n"
            "registry = ErrorRegistry(errors=[missing])\n"
            "router = CanonRouter(error_registry=registry)\n"
            "@router.get('/')\n"
            "def endpoint(): raise Missing()\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "fastapi_canon.check_error_contracts",
                str(root),
                "--strict",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 1, result.stderr
        report = json.loads(result.stdout)
        assert report["contract_gaps"] == 1
        assert report["incomplete_operations"] == 0
        source.write_text(
            source.read_text("utf-8").replace(
                "@router.get('/')", "@router.get('/', raises=[missing])"
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "fastapi_canon.check_error_contracts",
                str(root),
                "--strict",
                "--require-complete",
            ],
            check=True,
            timeout=30,
        )


def _cli(name: str) -> None:
    executable = Path(sys.executable).with_name(
        "canon.exe" if sys.platform == "win32" else "canon"
    )
    subprocess.run(
        [str(executable), name, "--help"], check=True, capture_output=True, timeout=30
    )


def _offline_sse_codegen() -> None:
    """Generate from a saved contract, including with codegen-only dependencies."""
    contract = (
        Path(__file__).resolve().parents[1]
        / "packages/fastapi-streams/tests/fixtures/models.openapi.json"
    )
    with tempfile.TemporaryDirectory(prefix="canon-offline-") as temporary:
        for language in ("python", "typescript"):
            command = [
                sys.executable,
                "-I",
                "-m",
                "fastapi_canon",
                "sse",
                "generate",
                "--schema",
                str(contract),
                "--language",
                language,
                "--output",
                str(Path(temporary) / language),
            ]
            subprocess.run(command, cwd=temporary, check=True, timeout=30)
            subprocess.run([*command, "--check"], cwd=temporary, check=True, timeout=30)


class _RecordingEngine:
    """Counts peers; admission must reject sessions before reaching it."""

    def __init__(self) -> None:
        from fastapi_canon.integrations.webrtc import EngineCapabilities

        self.capabilities = EngineCapabilities(
            trickle_ice=True, ice_restart=False, renegotiation=False
        )
        self.created = 0

    def track_kind(self, track):
        raise TypeError("No local tracks in the installed-artifact check")

    async def create_peer(self, config, observer):
        self.created += 1
        raise RuntimeError("The smoke engine allocates no peers")


def _webrtc_router(engine, *, canon=False):
    from fastapi import HTTPException, Query
    from fastapi_canon.integrations.webrtc import DataChannelSpec, Session, WebRtcRouter

    class AdmissionDenied(Exception):
        pass

    declarations = {}
    if canon:
        from fastapi_canon import Error, ErrorRegistry
        from fastapi_canon.integrations.webrtc import CanonWebRtcRouter

        denied = Error(
            AdmissionDenied,
            status=403,
            code="admission_denied",
            title="Denied",
            type="https://example.test/problems/admission_denied",
        )
        rtc = CanonWebRtcRouter(
            engine=engine, prefix="/rtc", error_registry=ErrorRegistry(errors=[denied])
        )
        declarations["raises"] = [denied]
    else:
        rtc = WebRtcRouter(engine=engine, prefix="/rtc")

    @rtc.session(
        "/rooms/{room}",
        data_channels=(DataChannelSpec(name="events", send_model=SocketOutput),),
        **declarations,
    )
    def room(room: str, token: str = Query()) -> Session:
        if token != "ok":
            if canon:
                raise AdmissionDenied()
            raise HTTPException(status_code=403)
        return Session()

    return rtc


def _webrtc_sessions(client, engine, *, canon=False) -> None:
    from fastapi_canon.integrations.webrtc import SUBPROTOCOL
    from starlette.testclient import WebSocketDenialResponse

    try:
        with client.websocket_connect("/rtc/rooms/a?token=no", [SUBPROTOCOL]):
            raise AssertionError("A denied session was accepted")
    except WebSocketDenialResponse as error:
        assert error.status_code == 403
        if canon:
            assert error.json()["code"] == "admission_denied"
    assert engine.created == 0
    with client.websocket_connect("/rtc/rooms/a?token=ok", [SUBPROTOCOL]) as socket:
        try:
            socket.receive_text()
        except WebSocketDisconnect as error:
            assert (error.code, error.reason) == (1011, "engine_failure")
        else:
            raise AssertionError("Expected an engine_failure close")
    assert engine.created == 1
    if importlib.util.find_spec("aiortc") is None:
        try:
            importlib.import_module("fastapi_canon.integrations.webrtc.engines.aiortc")
        except ImportError as error:
            assert "fastapi-canon[webrtc,aiortc]" in str(error)
        else:
            raise AssertionError("The aiortc engine imported without aiortc")
    else:
        from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine

        assert AiortcEngine().capabilities.renegotiation


def _webrtc_codegen(app) -> None:
    from fastapi_canon.integrations.webrtc.codegen.typescript import generate_typescript
    from fastapi_canon.integrations.webrtc.codegen.writer import write_files
    from fastapi_canon.integrations.webrtc.contract import build_contract

    contract = build_contract(app)
    (session,) = contract["sessions"]
    assert session["path"] == "/rtc/rooms/{room}"
    files = generate_typescript(contract)
    assert "export class RtcClient {" in files["client.ts"]
    assert "readonly room: SessionApi<" in files["client.ts"]
    with tempfile.TemporaryDirectory(prefix="canon-webrtc-client-") as temporary:
        output = Path(temporary) / "client"
        assert write_files(output, files)
        assert not write_files(output, files, check=True)


def _rpc_codegen(rpc) -> None:
    from fastapi_canon.integrations.websockets.codegen import (
        PythonClientOptions,
        TypeScriptClientOptions,
        generate_python_client,
        generate_typescript_client,
    )
    from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory

    document = rpc.contract(title="Health", base_url="ws://test").to_openrpc()
    with tempfile.TemporaryDirectory(prefix="canon-rpc-client-") as temporary:
        working = Path(temporary)
        python_options = PythonClientOptions(
            package="health_client",
            client_name="HealthClient",
            with_transport="websocket",
        )
        typescript_options = TypeScriptClientOptions(with_transport="websocket")
        generate_python_client(document, working / "health_client", python_options)
        generate_typescript_client(document, working / "typescript", typescript_options)
        assert not generate_python_client(
            document, working / "health_client", python_options, check=True
        )
        assert not generate_typescript_client(
            document, working / "typescript", typescript_options, check=True
        )
        sys.path.insert(0, temporary)
        try:
            generated = importlib.import_module("health_client")

            async def consume() -> None:
                async with (
                    InMemorySocketFactory(rpc) as factory,
                    generated.HealthClient.connect(
                        socket_factory=factory, socket_connection_factory=factory
                    ) as client,
                ):
                    assert await client.health.ping() == "pong"
                    async with client.health.messages() as socket:
                        await socket.send(generated.SocketInput(value=41))
                        result = await socket.receive()
                        assert isinstance(result, generated.SocketOutput)
                        assert result.value == 42
                    async with client.health.session() as socket:
                        assert (await socket.receive()).value == 0
                        await socket.send(generated.SocketInput(value=41))
                        assert (await socket.receive()).value == 42

            asyncio.run(consume())
        finally:
            sys.path.remove(temporary)
        node = shutil.which("node")
        compiler = (
            Path(__file__).resolve().parents[1]
            / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
        )
        assert node and compiler.is_file(), "Run npm ci for the Streams member first"
        subprocess.run(
            [
                node,
                str(compiler),
                "--strict",
                "--target",
                "ES2022",
                "--module",
                "ESNext",
                "--moduleResolution",
                "Bundler",
                "--lib",
                "ES2022,DOM,ESNext.Disposable",
                "--skipLibCheck",
                "--noEmit",
                "typescript/index.ts",
            ],
            cwd=working,
            check=True,
            timeout=30,
        )


def _router_policies() -> None:
    """Check the public policy API against installed wheels, including minima."""
    from fastapi_canon import (
        CanonRouter,
        Composition,
        Error,
        ErrorRegistry,
        Feature,
        RouterPolicy,
    )

    class AuthenticationRequired(Exception):
        pass

    class ItemDenied(Exception):
        pass

    authentication_required = Error(
        AuthenticationRequired,
        status=401,
        code="authentication_required",
        title="Authentication required",
    )
    item_denied = Error(ItemDenied, status=401, code="item_denied", title="Item denied")
    auth_errors = ErrorRegistry(
        errors=[authentication_required], type_base="https://example.test/problems"
    )
    item_errors = ErrorRegistry(
        errors=[item_denied], type_base="https://example.test/problems"
    )

    async def require_user(authorization: str | None = Header(default=None)) -> None:
        if authorization != "Bearer test":
            raise AuthenticationRequired

    policy = RouterPolicy(
        error_registry=auth_errors,
        dependencies=[Depends(require_user)],
        raises=[authentication_required],
    )
    for direct in (False, True):
        router = CanonRouter(
            policy=policy if direct else None, error_registry=item_errors
        )
        router.get("/policy", raises=[item_denied])(lambda: "ok")
        feature = Feature(name="policy", routers=[router])
        composition = Composition(feature, router_policy=None if direct else policy)
        app = composition.apply(FastAPI())
        composition.validate(app)
        with TestClient(app) as client:
            assert client.get("/policy").json()["code"] == "authentication_required"
            assert (
                client.get("/policy", headers={"Authorization": "Bearer test"}).json()
                == "ok"
            )
        response = app.openapi()["paths"]["/policy"]["get"]["responses"]["401"]
        assert (
            len(response["content"]["application/problem+json"]["schema"]["oneOf"]) == 2
        )
        if not direct:
            public = Composition(feature).apply(FastAPI())
            assert TestClient(public).get("/policy").status_code == 200


if __name__ == "__main__":
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        main()
    finally:
        faulthandler.cancel_dump_traceback_later()
