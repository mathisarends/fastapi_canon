import asyncio
import importlib
import shutil
import socket
import subprocess
import sys
import time
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
from fastapi_canon.integrations.sse.codegen.writer import write_files
from pydantic import ValidationError

from examples.agent_runs.generated_python import Client
from examples.agent_runs.generated_python.models import RunRequest
from tests.codegen.helpers import load_package
from tests.fixtures.integration_app import app

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def server():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.fixtures.integration_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(process.communicate()[1].decode())
                try:
                    if client.get(url + "/health", timeout=0.2).is_success:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Example server did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_generated_typescript_client_over_real_http(server):
    node = shutil.which("node")
    compiler = ROOT / "node_modules/typescript/bin/tsc"
    if not node or not compiler.exists():
        pytest.skip("Run npm ci to enable the TypeScript end-to-end acceptance test")
    subprocess.run(
        [node, str(compiler), "-p", "tests/typescript/tsconfig.json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = subprocess.run(
        [node, "tests/typescript/dist/tests/typescript/live-client.js", server],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert "generated Fetch client: passed" in result.stdout


async def test_generated_python_client_over_real_http(server):
    async with Client(server, headers={"Authorization": "Bearer demo-token"}) as client:
        events = [
            event
            async for event in client.agent_runs.stream_run_events(
                run_id="python", delay=0
            )
        ]
        assert events[0].type == "run.started"
        assert events[-1].type == "run.completed"
        assert (
            "".join(event.text for event in events if event.type == "token.delta")
            == "Hello 世界 👋"
        )
        events = [
            event
            async for event in client.agent_runs.stream_prompt(
                run_id="python", body=RunRequest(prompt="typed python")
            )
        ]
        assert len([event for event in events if event.type == "token.delta"]) == 2


@pytest.fixture(scope="module")
def current_clients(tmp_path_factory):
    root = tmp_path_factory.mktemp("current_fastapi_clients")
    contract = build_ir(export_schema(app))
    python = {
        backend: generate_python(contract, http_client=backend)
        for backend in ("httpx", "httpx2")
    }
    write_files(root / "generated", generate_typescript(contract))
    return root, python


async def assert_server_cleanup(http, server, stream_id):
    async with asyncio.timeout(3):
        while True:
            state = (await http.get(server + "/lifecycle/" + stream_id)).json()
            if "generator_closed" in state and "dependency_closed" in state:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["break", "cancel"])
async def test_current_canon_sse_immediately_and_closes_on_disconnect(
    server, current_clients, mode, python_backend
):
    root, python = current_clients
    stream_id = python_backend.__name__ + "_" + mode
    with load_package(root / stream_id, python[python_backend.__name__]) as sdk:
        async with (
            python_backend.AsyncClient(timeout=3, trust_env=False) as http,
            sdk.Client(server, http_client=http) as client,
        ):
            events = client.compat.open_stream(stream_id=stream_id)
            async with asyncio.timeout(3):
                async with aclosing(events):
                    async for event in events:
                        assert event.n == 1
                        assert (
                            await http.get(server + "/lifecycle/" + stream_id)
                        ).json() == ["dependency_open"]
                        if mode == "cancel":
                            task = asyncio.create_task(anext(events))
                            await asyncio.sleep(0)
                            task.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await task
                        break
            await assert_server_cleanup(http, server, stream_id)


async def test_current_generated_python_client_preserves_wire_values_and_validation(
    server, current_clients, python_backend
):
    root, python = current_clients
    with load_package(
        root / (python_backend.__name__ + "_wire"), python[python_backend.__name__]
    ) as sdk:
        async with sdk.Client(server) as client:
            events = [
                event
                async for event in client.fixtures.nested_events(
                    item_id=42, labels=["a b", "c&d"], x_trace="trace"
                )
            ]
            assert events[0].external_id == "42"
            assert events[0].metadata.labels == ["a b", "c&d"]
            assert events[0].metadata.note == "trace"
            assert [event.n async for event in client.compat.created()] == [2]
            with pytest.raises(ValidationError):
                client.agent_runs.stream_run_events(run_id="bad", delay=-1)
            async with python_backend.AsyncClient() as http:
                response = await http.get(
                    server + "/v1/runs/bad/events", params={"delay": -1}
                )
                assert response.status_code == 422
            with pytest.raises(sdk.HttpError) as raised:
                _ = [
                    event
                    async for event in client.agent_runs.stream_prompt(
                        run_id="denied", body=sdk.models.RunRequest(prompt="Hello")
                    )
                ]
            assert raised.value.status in (401, 403)
            events = [
                event
                async for event in client.agent_runs.stream_prompt(
                    run_id="authenticated",
                    body=sdk.models.RunRequest(prompt="Hello"),
                    options=sdk.RequestOptions(
                        headers={"Authorization": "Bearer demo-token"}
                    ),
                )
            ]
            assert events[-1].run_id == "authenticated"
            assert any(
                event.type == "token.delta" and event.text == "Hello"
                for event in events
            )


async def test_current_generated_python_nullable_parameters(
    server, current_clients, python_backend
):
    root, python = current_clients
    with load_package(
        root / (python_backend.__name__ + "_nullable"), python[python_backend.__name__]
    ) as sdk:
        async with sdk.Client(server) as client:
            for params, expected in [
                ({}, 0),
                ({"limit": None, "x_limit": None}, 0),
                ({"limit": 2, "x_limit": 3}, 5),
            ]:
                assert [
                    event.n
                    async for event in client.compat.nullable_parameters(**params)
                ] == [expected]
            for params, name in [
                ({"limit": None, "x_limit": 1}, "query parameter limit"),
                ({"limit": 1, "x_limit": None}, "header parameter x-limit"),
            ]:
                with pytest.raises(TypeError, match=name):
                    client.compat.required_nullable_parameters(**params)
            assert [
                event.n
                async for event in client.compat.required_nullable_parameters(
                    limit=2, x_limit=3
                )
            ] == [5]


def test_current_generated_typescript_client_streaming_and_cleanup(
    server, current_clients
):
    root, _ = current_clients
    node = shutil.which("node")
    assert node, "Node is required for client integration tests"
    shutil.copyfile(ROOT / "tests/fixtures/compat-client.ts", root / "compat-client.ts")
    (root / "package.json").write_text('{"type":"module"}')
    result = subprocess.run(
        [
            node,
            str(ROOT / "node_modules/typescript/bin/tsc"),
            "--strict",
            "--target",
            "ES2022",
            "--module",
            "NodeNext",
            "--moduleResolution",
            "NodeNext",
            "--typeRoots",
            str(ROOT / "node_modules/@types"),
            "--outDir",
            "dist",
            "compat-client.ts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    result = subprocess.run(
        [node, str(root / "dist/compat-client.js"), server],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(params=["httpx", "httpx2"])
def python_backend(request):
    return importlib.import_module(request.param)
