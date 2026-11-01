from __future__ import annotations

import asyncio
import copy
import importlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.canonical_errors.app import app, contract, endpoint
from fastapi_canon.cli import main
from fastapi_canon.codegen.config import (
    Configuration,
    SourceConfig,
    TargetConfig,
    load_config,
)
from fastapi_canon.codegen.generate import generate
from fastapi_canon.codegen.ir import GenerationError
from fastapi_canon.codegen.planner import build_plan
from fastapi_canon.codegen.render import render
from fastapi_canon.integrations.sse.codegen.openapi import export_schema


def test_unified_python_calls_streams_and_model_error_identity(tmp_path: Path) -> None:
    plan = build_plan(_inputs())
    files = render(
        plan,
        TargetConfig(
            "test", "python", ("api", "control"), tmp_path, package="unified_test"
        ),
    )
    root = tmp_path / "unified_test"
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        sdk: Any = importlib.import_module("unified_test")
        models = importlib.import_module("unified_test.models")
        rpc_models = importlib.import_module(
            "unified_test.internal.control_jsonrpc.models"
        )
        assert rpc_models.ProjectMissingProblem is models.ProjectMissingProblem
        assert rpc_models.Color is models.Color
        assert not any(name.endswith("problem.py") for name in files)

        async def exercise() -> None:
            module = importlib.import_module(
                "unified_test.internal.control_jsonrpc.transport"
            )
            client_module = importlib.import_module(
                "unified_test.internal.control_jsonrpc.client"
            )
            server = endpoint.create_server()
            queue: asyncio.Queue[str] = asyncio.Queue()
            closed = []

            class Socket:
                async def send(self, text: str) -> None:
                    response = await server.handle(json.loads(text))
                    assert response is not None and not isinstance(response, list)
                    await queue.put(response.model_dump_json())

                async def recv(self) -> str:
                    return await queue.get()

                async def close(self) -> None:
                    closed.append(True)

            rpc = client_module.BindingClient(module.WebSocketTransport(Socket()))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as http:
                async with sdk.ApiClient(
                    "http://test", http_client=http, clients={"control": rpc}
                ) as client:
                    assert (await client.projects.get(project_id=1)).id == 1
                    assert [
                        event.sequence
                        async for event in client.projects.events(project_id=1)
                    ] == [1]
                    for operation in (
                        lambda: client.projects.get(project_id=0),
                        lambda: _collect(client.projects.events(project_id=2)),
                        lambda: client.projects.cancel(project_id=0),
                    ):
                        with pytest.raises(sdk.ProblemError) as caught:
                            await operation()
                        assert (
                            caught.value.problem.type
                            == "https://api.example.com/problems/project_missing"
                        )
                        assert caught.value.problem.replacement is None
                        assert isinstance(
                            caught.value.problem, models.ProjectMissingProblem
                        )
                    await client.projects.cancel(project_id=1)
                assert not http.is_closed
                assert closed == []  # Injected binding belongs to the caller.
                await rpc.projects.cancel(project_id=1)
                await client.aclose()
                with pytest.raises(sdk.TransportError, match="closed"):
                    await client.projects.get(project_id=1)
            await rpc.close()
            assert closed == [True]

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    finally:
        sys.path.remove(str(tmp_path))
        for name in tuple(sys.modules):
            if name == "unified_test" or name.startswith("unified_test."):
                del sys.modules[name]


def test_unified_typescript_compiles_and_executes_all_bindings(tmp_path: Path) -> None:
    node = shutil.which("node")
    compiler = (
        Path(__file__).resolve().parents[4]
        / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
    )
    if node is None or not compiler.exists():
        pytest.skip("Run npm ci --ignore-scripts --prefix packages/fastapi-streams")
    inputs = _inputs()
    rpc = copy.deepcopy(inputs[1][1])
    rpc["x-rpckit-sockets"] = [
        {
            "name": "projects.openSession",
            "url": "wss://test/socket",
            "messageTypes": ["text", "binary"],
        }
    ]
    rpc["x-rpc-notifications"] = [
        {
            "name": "projects.updates",
            "payload": {"type": "integer"},
            "message": {"type": "integer"},
        }
    ]
    rtc: dict[str, Any] = {
        "contract_version": 1,
        "protocol": 1,
        "subprotocol": "fastapi-webrtc.v1",
        "definitions": {},
        "sessions": [
            {
                "name": "room",
                "path": "/room",
                "role": "answerer",
                "description": None,
                "parameters": [],
                "media": {"audio": None, "video": None, "max_incoming_tracks": 4},
                "channels": [],
            }
        ],
    }
    plan = build_plan(
        (
            inputs[0],
            (inputs[1][0], rpc),
            (SourceConfig("media", "webrtc", operations={"room": "rooms.join"}), rtc),
        )
    )
    with pytest.raises(GenerationError, match="no Python media engine"):
        render(
            plan,
            TargetConfig("python", "python", ("api", "control", "media"), tmp_path),
        )
    output = render(
        plan,
        TargetConfig("test", "typescript", ("api", "control"), tmp_path),
    )
    for name, content in output.items():
        target = tmp_path / "sdk" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = subprocess.run(
        [
            node,
            str(compiler),
            "--strict",
            "--target",
            "es2022",
            "--module",
            "commonjs",
            "--lib",
            "es2022,dom,esnext.disposable",
            "--outDir",
            str(tmp_path / "js"),
            str(tmp_path / "sdk/index.ts"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    runner = tmp_path / "test.cjs"
    runner.write_text(
        r"""
const assert = require("node:assert/strict");
const sdk = require("./js/index.js");
const rpcProblem = require("./js/internal/control_jsonrpc/errors.js");
const problem = {type:"https://api.example.com/problems/project_missing", title:"Project not found", code:"project_missing",replacement:null};
let opened = 0, closed = 0;
const socketFactory = url => {
  assert.equal(url, "wss://test/prefix/ws"); opened++;
  const listeners = new Map();
  const socket = {
    readyState:1,
    addEventListener:(name, listener) => listeners.set(name, listener),
    removeEventListener:name => listeners.delete(name),
    send:text => {const r = JSON.parse(text);queueMicrotask(() => listeners.get("message")?.({data:JSON.stringify({jsonrpc:"2.0",id:r.id,error:{code:-32000,message:"Missing",data:{problem}}})}));},
    close:() => {closed++;socket.readyState=3;listeners.get("close")?.({code:1000,reason:"done"});}
  }; return socket;
};
const client = sdk.createClient({baseUrl:"https://test/prefix", bindings:{control:{connect:{socketFactory}}}, fetch:async input => {
  const url = String(input);
  if(url.endsWith("/events")) return new Response(`data: {"sequence":1}\n\nevent: canon.problem\ndata: ${JSON.stringify(problem)}\n\n`,{headers:{"content-type":"text/event-stream"}});
  if(url.endsWith("/0")) return new Response(JSON.stringify({...problem,status:404}),{status:404,headers:{"content-type":"application/problem+json"}});
  return new Response('{"id":1}',{headers:{"content-type":"application/json"}});
}});
(async () => {
  assert.equal(opened,0);
  assert.deepEqual(await client.projects.get({projectId:1}),{id:1});
  await assert.rejects(client.projects.get({projectId:0}), sdk.ProblemError);
  assert.equal(opened,0);
  await assert.rejects(async()=>{for await (const event of client.projects.events({projectId:2})) assert.equal(event.sequence,1);},sdk.ProblemError);
  await assert.rejects(client.projects.cancel({projectId:0}),sdk.ProblemError);
  assert(rpcProblem.errorFromResponse(-32000,"","",undefined,problem) instanceof sdk.ProblemError);
  await client.close(); await client.close(); assert.equal(closed,1);
  await assert.rejects(client.projects.get({projectId:1}),sdk.TransportError);
})().catch(e=>{console.error(e);process.exitCode=1;});
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generation_is_offline_deterministic_owned_and_preflighted(
    tmp_path: Path,
) -> None:
    inputs = _inputs()
    for source, document in inputs:
        (tmp_path / (source.name + ".json")).write_text(
            json.dumps(document), encoding="utf-8"
        )
    config = Configuration(
        tmp_path / "fastapi-canon.toml",
        tuple(
            replace(source, input=tmp_path / (source.name + ".json"))
            for source, _ in inputs
        ),
        (TargetConfig("python", "python", ("api", "control"), tmp_path / "sdk"),),
    )
    assert generate(config)[0]
    assert not generate(config, offline=True, check=True)[0]
    handwritten = tmp_path / "sdk/handwritten.py"
    handwritten.write_text("preserve", encoding="utf-8")
    client = tmp_path / "sdk/client.py"
    original = client.read_text(encoding="utf-8")
    client.write_text("drift", encoding="utf-8")
    assert generate(config, check=True, diff=True)[0]
    assert client.read_text(encoding="utf-8") == "drift"
    generate(config)
    assert client.read_text(encoding="utf-8") == original
    assert handwritten.read_text(encoding="utf-8") == "preserve"
    bad = replace(
        config,
        targets=(
            *config.targets,
            TargetConfig("bad", "python", ("api", "control"), tmp_path / "unowned"),
        ),
    )
    (tmp_path / "unowned").mkdir()
    (tmp_path / "unowned/client.py").write_text("mine", encoding="utf-8")
    client.write_text("preflight", encoding="utf-8")
    with pytest.raises(GenerationError, match="unowned"):
        generate(bad)
    assert client.read_text(encoding="utf-8") == "preflight"


def test_model_and_operation_conflicts_require_explicit_resolution() -> None:
    api = export_schema(app)
    duplicate = copy.deepcopy(api)
    with pytest.raises(GenerationError, match="collides"):
        build_plan(
            (
                (SourceConfig("one", "openapi"), api),
                (SourceConfig("two", "openapi"), duplicate),
            )
        )
    identities = {name: "urn:test:" + name for name in api["components"]["schemas"]}
    with pytest.raises(GenerationError, match="operation conflict"):
        build_plan(
            (
                (SourceConfig("one", "openapi", identities=identities), api),
                (SourceConfig("two", "openapi", identities=identities), duplicate),
            )
        )
    config = SourceConfig("api", "openapi", operations={"get": "projects.read"})
    assert any(
        operation.name == ("projects", "read")
        for operation in build_plan(((config, api),)).operations
    )
    rpc = contract.to_openrpc()
    rpc["components"]["schemas"]["ProjectMissingProblem"]["properties"][
        "replacement"
    ] = {"type": "integer"}
    with pytest.raises(GenerationError, match="Incompatible wire schemas"):
        build_plan(
            (
                (SourceConfig("api", "openapi"), api),
                (SourceConfig("control", "openrpc"), rpc),
            )
        )

    documents = [
        {
            "openapi": "3.1.0",
            "components": {
                "schemas": {
                    "Report": {
                        "type": "object",
                        "x-canon-model-id": "urn:test:report",
                        "properties": {"title": {"type": kind}},
                    }
                }
            },
        }
        for kind in ("string", "integer")
    ]
    with pytest.raises(GenerationError, match="Incompatible wire schemas"):
        build_plan(
            (
                (SourceConfig("one", "openapi"), documents[0]),
                (SourceConfig("two", "openapi"), documents[1]),
            )
        )


def test_example_configuration_loads() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "examples/canonical_errors/fastapi-canon.toml"
    )
    config = load_config(path)
    assert {target.language for target in config.targets} == {"python", "typescript"}


@pytest.mark.parametrize("filename", ["fastapi-canon.toml", "pyproject.toml"])
def test_codegen_configuration_discovery_and_package_namespace(
    filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "openapi.json").write_text(
        json.dumps(export_schema(app)), encoding="utf-8"
    )
    section = (
        "tool.fastapi-canon.codegen" if filename == "pyproject.toml" else "codegen"
    )
    configuration = f"""[{section}]
version = 1
[{section}.sources.api]
kind = "openapi"
input = "openapi.json"
[{section}.targets.browser]
language = "typescript"
sources = ["api"]
output = "generated"
"""
    if filename == "pyproject.toml":
        configuration = '[project]\nname = "example"\nversion = "9.0"\n' + configuration
    else:
        # The dedicated file takes precedence over unrelated project metadata.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "example"\nversion = "9.0"\n', encoding="utf-8"
        )
    (tmp_path / filename).write_text(configuration, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["generate", "--offline"]) == 0
    assert (tmp_path / "generated/index.ts").is_file()
    assert main(["generate", "--offline", "--check"]) == 0


@pytest.mark.asyncio
async def test_http_binding_preserves_absent_and_null_json_and_parameter_encoding(
    tmp_path: Path,
) -> None:
    operation = {
        "operationId": "things.create",
        "parameters": [
            {
                "name": "slug",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "q",
                "in": "query",
                "schema": {"type": "array", "items": {"type": "boolean"}},
            },
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            {
                                "type": "object",
                                "additionalProperties": {"type": "integer"},
                            },
                            {"type": "null"},
                        ]
                    }
                }
            }
        },
        "responses": {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": {"type": "string"}}},
            }
        },
    }
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Things", "version": "1"},
        "paths": {"/items/{slug}": {"post": operation}},
    }
    files = render(
        build_plan(((SourceConfig("api", "openapi"), document),)),
        TargetConfig("python", "python", ("api",), tmp_path, package="http_sdk"),
    )
    for name, content in files.items():
        path = tmp_path / "http_sdk" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        sdk: Any = importlib.import_module("http_sdk")

        async with asyncio.timeout(15):

            def respond(request: httpx.Request) -> httpx.Response:
                assert request.url.raw_path.startswith(b"/items/a%2Fb%3F?")
                assert request.url.params.get_list("q") == ["true", "false"]
                return httpx.Response(200, json=request.content.decode())

            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
                sdk.ApiClient("https://test", http_client=http) as client,
            ):
                assert await client.things.create(slug="a/b?", q=[True, False]) == ""
                assert (
                    await client.things.create(slug="a/b?", q=[True, False], body=None)
                    == "null"
                )
                assert json.loads(
                    await client.things.create(
                        slug="a/b?", q=[True, False], body={"n": 3}
                    )
                ) == {"n": 3}

    finally:
        sys.path.remove(str(tmp_path))
        for name in tuple(sys.modules):
            if name == "http_sdk" or name.startswith("http_sdk."):
                del sys.modules[name]


def test_canonical_names_wire_aliases_and_primitive_results(tmp_path: Path) -> None:
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime
    from enum import StrEnum
    from typing import Literal

    from fastapi import FastAPI
    from pydantic import BaseModel, ConfigDict
    from pydantic.alias_generators import to_camel

    from fastapi_canon import CanonRouter, Composition, Feature
    from fastapi_canon.integrations.sse import CanonStreamRouter
    from fastapi_canon.integrations.websockets import RpcChannel, RpcService

    class APIState(StrEnum):
        READY = "HTTPStatus"
        MODEL = "model_id"

    class APIRecord(BaseModel):
        model_config = ConfigDict(
            extra="forbid",
            alias_generator=to_camel,
            populate_by_name=True,
            json_schema_extra={"x-canon-model-id": "urn:test:api-record"},
        )
        model_id: int
        http_status: str
        state: APIState

    APIRecord.model_rebuild(_types_namespace={"APIState": APIState})
    value = APIRecord(model_id=7, http_status="ok", state=APIState.READY)
    http_router = CanonRouter()
    streams = CanonStreamRouter()
    channel = RpcChannel("models")

    async def get_record() -> Any:
        return value

    get_record.__annotations__["return"] = APIRecord
    http_router.get("/record", operation_id="get", tags=["models"])(get_record)

    async def watch() -> Any:
        yield value

    watch.__annotations__["return"] = AsyncIterator[APIRecord]
    streams.stream("/records", operation_id="watchHTTPRecords", tags=["HTTPModels"])(
        watch
    )

    async def update(params: Any) -> Any:
        return params

    update.__annotations__ = {"params": APIRecord, "return": APIRecord}
    channel.method("setHTTPRecord")(update)

    async def started_at() -> Any:
        return datetime(2026, 10, 31, tzinfo=UTC)

    started_at.__annotations__["return"] = datetime
    channel.method()(started_at)

    async def state() -> Any:
        return APIState.READY

    state.__annotations__["return"] = Literal[APIState.READY]
    channel.method()(state)
    service = RpcService()
    endpoint = service.socket("/ws", channels=[channel])
    application = Composition(
        Feature(name="records", routers=[http_router, streams])
    ).apply(FastAPI())
    inputs = (
        (
            SourceConfig(
                "api",
                "openapi",
                operations={"watchHTTPRecords": "models.watch"},
                identities={"APIState": "urn:test:api-state"},
            ),
            export_schema(application),
        ),
        (
            SourceConfig(
                "control", "openrpc", identities={"APIState": "urn:test:api-state"}
            ),
            service.contract(title="Records", base_url="ws://test").to_openrpc(),
        ),
    )
    files = render(
        build_plan(inputs),
        TargetConfig(
            "python", "python", ("api", "control"), tmp_path, package="names_sdk"
        ),
    )
    node = shutil.which("node")
    compiler = (
        Path(__file__).resolve().parents[4]
        / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
    )
    if node is not None and compiler.exists():
        for name, text in render(
            build_plan(inputs),
            TargetConfig("browser", "typescript", ("api", "control"), tmp_path),
        ).items():
            path = tmp_path / "ts" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        compiled = subprocess.run(
            [
                node,
                str(compiler),
                "--strict",
                "--target",
                "es2022",
                "--module",
                "commonjs",
                "--lib",
                "es2022,dom,esnext.disposable",
                "--noEmit",
                str(tmp_path / "ts/index.ts"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    for name, text in files.items():
        path = tmp_path / "names_sdk" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        sdk: Any = importlib.import_module("names_sdk")
        models: Any = importlib.import_module("names_sdk.models")
        client_module: Any = importlib.import_module(
            "names_sdk.internal.control_jsonrpc.client"
        )
        transport_module: Any = importlib.import_module(
            "names_sdk.internal.control_jsonrpc.transport"
        )

        async def exercise() -> None:
            server = endpoint.create_server()
            queue: asyncio.Queue[str] = asyncio.Queue()

            class Socket:
                async def send(self, message: str) -> None:
                    response = await server.handle(json.loads(message))
                    assert response is not None and not isinstance(response, list)
                    await queue.put(response.model_dump_json())

                async def recv(self) -> str:
                    return await queue.get()

                async def close(self) -> None:
                    pass

            binding = client_module.BindingClient(
                transport_module.WebSocketTransport(Socket())
            )
            async with (
                httpx.AsyncClient(transport=httpx.ASGITransport(application)) as http,
                sdk.ApiClient(
                    "http://test", http_client=http, clients={"control": binding}
                ) as client,
            ):
                one = await client.models.get()
                two = await client.models.set_http_record(
                    model_id=7, http_status="ok", state="HTTPStatus"
                )
                records = [record async for record in client.models.watch()]
                assert len(records) == 1
                three = records.pop()
                assert all(
                    isinstance(record, models.ApiRecord) for record in (one, two, three)
                )
                assert one.model_id == two.model_id == three.model_id == 7
                assert one.http_status == two.http_status == three.http_status == "ok"
                assert await client.models.started_at() == "2026-10-31T00:00:00Z"
                assert await client.models.state() == "HTTPStatus"
            await binding.close()

        asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    finally:
        sys.path.remove(str(tmp_path))
        for name in tuple(sys.modules):
            if name == "names_sdk" or name.startswith("names_sdk."):
                del sys.modules[name]


def test_terminal_only_nested_problem_definitions_share_identity(
    tmp_path: Path,
) -> None:
    import ast
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from pydantic import BaseModel

    from fastapi_canon import Composition, Error, ErrorRegistry, Feature
    from fastapi_canon.integrations.sse import CanonStreamRouter

    class Leaf(BaseModel):
        count: int

    class Meta(BaseModel):
        leaf: Leaf

    Meta.model_rebuild(_types_namespace={"Leaf": Leaf})

    class Fields(BaseModel):
        metadata: Meta

    # Resolve local annotations for generator reflection.
    Fields.model_rebuild(_types_namespace={"Meta": Meta})

    class Missing(Exception):
        pass

    missing = Error(
        Missing,
        status=404,
        code="nested_missing",
        title="Missing",
        extensions_model=Fields,
        extensions={"metadata": {"leaf": {"count": 1}}},
    )
    errors = ErrorRegistry(errors=[missing], type_base="https://test/problems")
    streams = CanonStreamRouter(error_registry=errors)

    async def nested() -> AsyncIterator[int]:
        yield 1
        raise Missing()

    # Avoid local forward-reference names in the operation signature.
    nested.__annotations__["return"] = AsyncIterator[int]
    streams.stream(
        "/nested", operation_id="nested", tags=["projects"], stream_raises=[missing]
    )(nested)
    application = Composition(Feature(name="nested", routers=[streams])).apply(
        FastAPI()
    )
    plan = build_plan(((SourceConfig("api", "openapi"), export_schema(application)),))
    assert (
        sum(
            model.problem_type == "https://test/problems/nested_missing"
            for model in plan.models
        )
        == 1
    )
    assert any(
        model.identity == "https://test/problems/nested_missing#/$defs/Meta"
        for model in plan.models
    )
    for name, content in render(
        plan, TargetConfig("python", "python", ("api",), tmp_path)
    ).items():
        if name.endswith(".py"):
            ast.parse(content)


def _inputs() -> tuple[tuple[SourceConfig, dict[str, Any]], ...]:
    api, rpc = copy.deepcopy(export_schema(app)), contract.to_openrpc()
    color = {
        "title": "Color",
        "type": "string",
        "enum": ["red", "blue"],
        "x-canon-model-id": "urn:test:color",
    }
    api["components"]["schemas"]["Color"] = color
    rpc["components"]["schemas"]["Color"] = copy.deepcopy(color)
    api["components"]["schemas"]["ProjectEvent"]["properties"]["color"] = {
        "$ref": "#/components/schemas/Color"
    }
    rpc["components"]["schemas"]["CancelProject"]["properties"]["color"] = {
        "$ref": "#/components/schemas/Color"
    }
    rpc["methods"][0]["params"].append(
        {
            "name": "color",
            "required": False,
            "schema": {"$ref": "#/components/schemas/Color"},
        }
    )
    return (
        (SourceConfig("api", "openapi"), api),
        (SourceConfig("control", "openrpc"), rpc),
    )


async def _collect(stream: Any) -> list[Any]:
    return [item async for item in stream]
