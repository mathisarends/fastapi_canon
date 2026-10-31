import asyncio
import importlib
import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from examples.canonical_errors.app import contract, create_app, project_missing
from fastapi_canon import Composition, ErrorOptions, ErrorRegistry, Feature
from fastapi_canon.integrations.sse.codegen.ir import build_ir as stream_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.adapters.fastapi import RpcRoutes
from fastapi_canon.integrations.websockets.codegen import (
    PythonClientOptions,
    render_python_client,
)


def test_empty_catalog_keeps_its_configured_internal_problem_identity() -> None:
    from fastapi import FastAPI

    from examples.canonical_errors.app import ProjectEvent
    from fastapi_canon.integrations.sse import CanonStreamRouter

    router = CanonStreamRouter(
        error_registry=ErrorRegistry(errors=[], type_base="urn:empty:problems")
    )

    @router.stream("/events")
    async def events() -> AsyncIterator[ProjectEvent]:
        yield ProjectEvent(sequence=1)
        raise ValueError("private")

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/events")
    assert "urn:empty:problems/internal_server_error" in response.text
    assert "private" not in response.text


def test_same_domain_error_across_http_stream_and_rpc() -> None:
    app = create_app()
    with TestClient(app) as client:
        http_problem = client.get("/projects/0").json()
        terminal = client.get("/projects/2/events")
        frame = terminal.text.split("event: canon.problem\n", 1)[1]
        stream_problem = json.loads(frame.split("data: ", 1)[1])
        with client.websocket_connect("/ws") as socket:
            socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "projects.cancel",
                    "params": {"projectId": 0},
                }
            )
            response = socket.receive_json()
    rpc_problem = response["error"]["data"]["problem"]
    assert response["error"]["code"] == -32000
    assert response["error"]["message"] == "Project not found"
    assert response["error"]["data"] == {"problem": stream_problem}
    assert (
        {k: v for k, v in http_problem.items() if k != "status"}
        == stream_problem
        == rpc_problem
    )
    assert http_problem["status"] == 404
    assert "status" not in stream_problem
    assert "private" not in terminal.text
    assert "canon.complete" not in terminal.text
    assert stream_problem["replacement"] is None
    schema = contract.to_openrpc()["components"]["schemas"]["ProjectMissingProblem"]
    Draft202012Validator(schema).validate(rpc_problem)
    operation = app.openapi()["paths"]["/projects/{project_id}/events"]["get"]
    assert operation["x-canon-stream"]["version"] == 1
    assert "errorIds" not in operation["x-canon-stream"]


def test_rpc_router_contributes_catalog_and_uses_composition_type_base() -> None:
    from fastapi import APIRouter, FastAPI

    local = ErrorRegistry(errors=[project_missing])
    channel = RpcChannel("projects", error_registry=local)

    @channel.method(raises=[project_missing])
    async def missing() -> None:
        from examples.canonical_errors.app import ProjectMissing

        raise ProjectMissing("secret")

    service = RpcService()
    endpoint: Any = service.socket("/ws", channels=[channel])
    router = APIRouter()
    RpcRoutes(router).mount(endpoint)
    app = Composition(
        Feature(name="projects", routers=[router]),
        errors=ErrorOptions(type_base="urn:application:problems"),
    ).apply(FastAPI())
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_json({"jsonrpc": "2.0", "id": 1, "method": "projects.missing"})
        problem = socket.receive_json()["error"]["data"]["problem"]
    assert problem["type"] == "urn:application:problems/project_missing"
    document = service.contract(
        title="Composed", base_url="wss://example.test", app=app
    ).to_openrpc()
    assert (
        document["methods"][0]["errors"][0]["x-canon-problem-type"] == problem["type"]
    )


def test_nested_public_extensions_keep_aliases_and_valid_contract_references() -> None:
    from pydantic import BaseModel, Field

    from fastapi_canon import Error

    class Retry(BaseModel):
        retry_after: int = Field(alias="retryAfter")

    class PublicFields(BaseModel):
        recovery: Retry

    class Missing(Exception):
        pass

    def public_fields(_: Missing) -> PublicFields:
        return PublicFields(recovery=Retry(retryAfter=5))

    error = Error(
        Missing,
        status=404,
        code="rich_missing",
        title="Missing",
        extensions_model=PublicFields,
        extensions=public_fields,
    )
    registry = ErrorRegistry(errors=[error], type_base="urn:test:problems")
    channel = RpcChannel("items", error_registry=registry)

    @channel.method(raises=[error])
    async def missing() -> None:
        raise Missing("private")

    service = RpcService()
    service.socket("/ws", channels=[channel])
    document = service.contract(title="Nested", base_url="wss://test").to_openrpc()
    server = channel.create_server()

    async def render() -> Any:
        response = await server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "items.missing"}
        )
        assert response is not None and not isinstance(response, list)
        return response.model_dump(mode="json")["error"]["data"]["problem"]

    payload = asyncio.run(render())
    assert payload["recovery"] == {"retryAfter": 5}
    assert "private" not in json.dumps(payload)
    schema = {
        "$ref": "#/components/schemas/RichMissingProblem",
        "components": document["components"],
    }
    Draft202012Validator(schema).validate(payload)
    with pytest.raises(SchemaValidationError) as caught:
        Draft202012Validator(schema).validate(
            {**payload, "recovery": {"retryAfter": "bad"}}
        )
    assert "not of type" in str(caught.value)


def test_generated_python_stream_consumes_controls_and_exposes_typed_problem(
    tmp_path: Path,
) -> None:
    app = create_app()
    files = generate_python(stream_ir(export_schema(app)))
    with _sdk(tmp_path, "canonical_sse_sdk", files) as sdk:

        async def consume() -> None:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as http,
                sdk.Client("http://test", http_client=http) as client,
            ):
                values = [value async for value in client.projects.events(project_id=1)]
                assert [value.sequence for value in values] == [1]
                with pytest.raises(sdk.ProblemError) as caught:
                    _ = [value async for value in client.projects.events(project_id=2)]
                assert caught.value.problem.replacement is None
                assert caught.value.problem.code == "project_missing"
                assert (
                    type(caught.value.problem).__name__
                    == "ProjectMissingProblemEmbedded"
                )
                with pytest.raises(sdk.ProblemError) as http_error:
                    _ = [value async for value in client.projects.events(project_id=0)]
                assert http_error.value.problem.status == 404

        asyncio.run(consume())


def test_python_stream_rejects_incomplete_or_malformed_controls_and_cleans_up(
    tmp_path: Path,
) -> None:
    files = generate_python(stream_ir(export_schema(create_app())))
    with _sdk(tmp_path, "canonical_sse_sdk", files) as sdk:

        async def consume() -> None:
            for wire, error_type in [
                ('data: {"sequence":1}\n\n', sdk.TransportError),
                ("event: canon.complete\ndata: null\n\n", sdk.ProtocolError),
                ('event: canon.problem\ndata: {"title":"bad"}\n\n', sdk.ProtocolError),
                ("event: canon.future\ndata: {}\n\n", sdk.ProtocolError),
                ("event: canon.complete\ndata: {}", sdk.TransportError),
            ]:
                response = httpx.Response(
                    200, text=wire, headers={"content-type": "text/event-stream"}
                )
                async with (
                    httpx.AsyncClient(
                        transport=httpx.MockTransport(lambda _, reply=response: reply)
                    ) as http,
                    sdk.Client("https://test", http_client=http) as client,
                ):
                    with pytest.raises(error_type):
                        _ = [
                            value
                            async for value in client.projects.events(project_id=1)
                        ]
                assert response.is_closed

        asyncio.run(consume())


def test_stream_shared_declarations_and_safe_renderer_failures() -> None:
    from fastapi import FastAPI

    from examples.canonical_errors.app import ProjectEvent, ProjectMissing
    from fastapi_canon import Error
    from fastapi_canon.integrations.sse import CanonStreamRouter

    def bad_detail(_: ProjectMissing) -> str:
        raise ValueError("private renderer failure")

    error = Error(
        ProjectMissing,
        status=404,
        code="project_missing",
        title="Missing",
        detail=bad_detail,
    )
    registry = ErrorRegistry(errors=[error], type_base="urn:test:problems")
    router = CanonStreamRouter(error_registry=registry, stream_raises=[error])
    closed = []

    @router.stream("/events", stream_raises=[error])
    def events() -> Iterator[ProjectEvent]:
        try:
            yield ProjectEvent(sequence=1)
            raise ProjectMissing("private exception")
        finally:
            closed.append(True)

    app = Composition(Feature(name="events", routers=[router])).apply(FastAPI())
    response = TestClient(app).get("/events")
    assert closed == [True]
    assert "canon.problem" in response.text and "canon.complete" not in response.text
    assert "internal_server_error" in response.text and "private" not in response.text
    assert (
        len(app.openapi()["paths"]["/events"]["get"]["x-canon-stream"]["problems"]) == 1
    )


def test_cancelled_server_stream_runs_cleanup_without_terminal_problem() -> None:
    from fastapi import FastAPI

    from examples.canonical_errors.app import ProjectEvent, errors
    from fastapi_canon.integrations.sse import CanonStreamRouter

    router = CanonStreamRouter(error_registry=errors)
    closed = []

    @router.stream("/events")
    async def events() -> AsyncIterator[ProjectEvent]:
        try:
            yield ProjectEvent(sequence=1)
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    app = Composition(Feature(name="events", routers=[router])).apply(FastAPI())

    async def disconnect() -> None:
        started = asyncio.Event()
        messages = []
        first = True

        async def receive() -> Any:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message: Any) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                started.set()

        scope: Any = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1),
            "http_version": "1.1",
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert closed == [True]
        assert b"canon.problem" not in b"".join(
            message.get("body", b"") for message in messages
        )
        assert b"canon.complete" not in b"".join(
            message.get("body", b"") for message in messages
        )

    asyncio.run(disconnect())


def test_generated_python_rpc_error_uses_type_uri_and_preserves_extensions(
    tmp_path: Path,
) -> None:
    files = dict(
        render_python_client(
            contract.to_openrpc(),
            PythonClientOptions(
                package="canonical_rpc_sdk", with_transport="websocket"
            ),
        )
    )
    payload = {
        "type": "https://api.example.com/problems/project_missing",
        "title": "Project not found",
        "code": "project_missing",
        "replacement": None,
        "future": {"new": True},
    }
    with _sdk(tmp_path, "canonical_rpc_sdk", files) as sdk:
        errors = importlib.import_module("canonical_rpc_sdk.errors")
        error = errors.error_from_response(
            -32000, "", "Project not found", None, payload
        )
        assert isinstance(error, sdk.ProblemError)
        assert isinstance(error, sdk.ProjectMissingError)
        assert error.problem.replacement is None
        assert error.problem.model_dump()["future"] == {"new": True}
        unknown = errors.error_from_response(
            -32000,
            "",
            "Future problem",
            None,
            {**payload, "type": "urn:future:problem"},
        )
        assert isinstance(unknown, sdk.ProblemError)
        assert not isinstance(unknown, sdk.ProjectMissingError)
        with pytest.raises(sdk.ProtocolError):
            errors.error_from_response(
                -32000, "", "Invalid", None, {**payload, "status": 404}
            )

        async def request() -> None:
            from examples.canonical_errors.app import endpoint

            transport_module = importlib.import_module("canonical_rpc_sdk.transport")
            queue: asyncio.Queue[str] = asyncio.Queue()
            server = endpoint.create_server()

            class Socket:
                async def send(self, message: str) -> None:
                    response = await server.handle(json.loads(message))
                    assert response is not None and not isinstance(response, list)
                    await queue.put(response.model_dump_json())

                async def recv(self) -> str:
                    return await queue.get()

                async def close(self) -> None:
                    pass

            transport = transport_module.WebSocketTransport(Socket())
            client = sdk.ProjectsClient(transport)
            try:
                with pytest.raises(sdk.ProjectMissingError) as caught:
                    await client.projects.cancel(project_id=0)
                assert isinstance(caught.value, sdk.ProblemError)
                assert caught.value.problem.replacement is None
                await client.projects.cancel(project_id=1)
            finally:
                await client.close()

        asyncio.run(request())


def test_generated_typescript_clients_execute_problem_and_stream_lifecycle(
    tmp_path: Path,
) -> None:
    import shutil
    import subprocess

    from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
    from fastapi_canon.integrations.websockets.codegen import (
        TypeScriptClientOptions,
        render_typescript_client,
    )

    node = shutil.which("node")
    compiler = (
        Path(__file__).parents[4]
        / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
    )
    if node is None or not compiler.exists():
        pytest.skip("TypeScript execution requires the workspace Node dependencies")
    sdk_files = {
        "sse": generate_typescript(
            stream_ir(export_schema(create_app()), target="typescript")
        ),
        "rpc": dict(
            render_typescript_client(
                contract.to_openrpc(),
                TypeScriptClientOptions(
                    client_name="ProjectsClient", with_transport="websocket"
                ),
            )
        ),
    }
    paths = []
    for sdk, files in sdk_files.items():
        for name, text in files.items():
            if not name.endswith(".ts"):
                continue
            target = tmp_path / sdk / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            paths.append(str(target))
    result = subprocess.run(
        [
            node,
            str(compiler),
            "--target",
            "es2022",
            "--module",
            "commonjs",
            "--lib",
            "es2022,dom,esnext.disposable",
            "--strict",
            "--skipLibCheck",
            "--outDir",
            str(tmp_path / "js"),
            *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    runner = tmp_path / "execute.cjs"
    runner.write_text(
        r"""const assert = require("node:assert/strict");
const sse = require("./js/sse/index.js");
const rpc = require("./js/rpc/index.js");
const rpcErrors = require("./js/rpc/errors.js");
const payload = {type: "https://api.example.com/problems/project_missing", title: "Project not found", code: "project_missing", replacement: null, future: true};
const collect = async (stream) => { const values = []; for await (const v of stream) values.push(v); return values; };
const client = (wire, status = 200, type = "text/event-stream") => sse.createClient({baseUrl: "https://test", fetch: async () => new Response(wire, {status, headers: {"content-type": type}})});
(async () => {
  assert.deepEqual(await collect(client('data: {"sequence":1}\n\nevent: canon.complete\ndata: {}\n\n').projects.events({project_id: 1})), [{sequence:1}]);
  await assert.rejects(collect(client(`event: canon.problem\ndata: ${JSON.stringify(payload)}\n\n`).projects.events({project_id:2})), e => e instanceof sse.ProblemError && e.problem.replacement === null && e.problem.future === true);
  await assert.rejects(collect(client('data: {"sequence":1}\n\n').projects.events({project_id:1})), sse.TransportError);
  await assert.rejects(collect(client('event: canon.complete\ndata: null\n\n').projects.events({project_id:1})), sse.ProtocolError);
  await assert.rejects(collect(client(JSON.stringify({...payload,status:404}),404,"application/problem+json").projects.events({project_id:0})), e => e instanceof sse.ProblemError && e.problem.status === 404);
  const error = rpcErrors.errorFromResponse(-32000, "", "Missing", undefined, payload);
  const listeners = new Map();
  const socket = {
    readyState: 1,
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name) => listeners.delete(name),
    send: text => {
      const request = JSON.parse(text);
      queueMicrotask(() => listeners.get("message")?.({data:JSON.stringify({jsonrpc:"2.0",id:request.id,error:{code:-32000,message:"Missing",data:{problem:payload}}})}));
    },
    close: () => { socket.readyState = 3; listeners.get("close")?.({code:1000,reason:"done"}); },
  };
  const transport = await rpc.WebSocketTransport.open("wss://test", {socketFactory: () => socket});
  const rpcClient = new rpc.ProjectsClient(transport);
  await assert.rejects(rpcClient.projects.cancel({projectId:0}), e => e instanceof rpc.ProblemError && e instanceof rpc.ProjectMissingError);
  await rpcClient.close();
  assert(error instanceof rpc.ProblemError);
  assert(error instanceof rpc.ProjectMissingError);
  assert.equal(error.problem.replacement, null);
  assert.throws(() => rpcErrors.errorFromResponse(-32000,"","Bad",undefined,{...payload,status:404}),rpc.ProtocolError);
})().catch(e => { console.error(e); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _sdk(root: Path, name: str, files: dict[str, str]) -> Iterator[Any]:
    destination = root / name
    for path, content in files.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        yield importlib.import_module(name)
    finally:
        sys.path.remove(str(root))
        for module in tuple(sys.modules):
            if module == name or module.startswith(name + "."):
                del sys.modules[module]
