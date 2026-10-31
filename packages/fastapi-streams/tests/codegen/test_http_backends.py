import importlib
import json
import subprocess
import sys

import pytest
from fastapi_canon.integrations.sse.codegen.cli import main
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import ContractError, export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.writer import write_files

from examples.agent_runs import app
from tests.codegen.helpers import load_package


def test_httpx_remains_default_and_httpx2_is_explicit():
    contract = build_ir(export_schema(app))
    default = generate_python(contract)
    assert default == generate_python(contract, http_client="httpx")
    alternative = generate_python(contract, http_client="httpx2")
    for path in ("client.py", "runtime.py"):
        assert "\nimport httpx\n" in default[path]
        assert "httpx2" not in default[path]
        assert "\nimport httpx2 as httpx\n" in alternative[path]
    assert default["models.py"] == alternative["models.py"]
    with pytest.raises(ContractError, match="Unsupported Python HTTP client"):
        generate_python(contract, http_client="requests")


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
def test_cli_selects_backend_and_detects_backend_drift(tmp_path, backend):
    schema = tmp_path / "openapi.json"
    schema.write_text(json.dumps(export_schema(app)), encoding="utf-8")
    args = [
        "--schema",
        str(schema),
        "--language",
        "python",
        "--output",
        str(tmp_path / "sdk"),
    ]
    selected = [*args, "--python-http-client", backend]
    assert main(["generate", *selected]) == 0
    assert main(["check", "--app", "examples.agent_runs:app", *selected]) == 0
    assert main(["check", *args]) == (0 if backend == "httpx" else 1)
    assert main(["generate", "--check", *selected]) == 0


def test_typescript_rejects_python_backend_option(tmp_path, capsys):
    assert (
        main(
            [
                "generate",
                "--language",
                "typescript",
                "--python-http-client",
                "httpx2",
                "--schema",
                "missing.json",
                "--output",
                str(tmp_path / "sdk"),
            ]
        )
        == 2
    )
    assert "requires --language python" in capsys.readouterr().err
    assert not (tmp_path / "sdk").exists()


def test_client_types_accept_only_the_selected_native_family(tmp_path):
    contract = build_ir(export_schema(app))
    for backend in ("httpx", "httpx2"):
        write_files(
            tmp_path / ("sdk_" + backend),
            generate_python(contract, http_client=backend),
        )
    (tmp_path / "consumer.py").write_text(
        "import httpx\nimport httpx2\n"
        "from sdk_httpx import Client as HttpxClient\n"
        "from sdk_httpx2 import Client as Httpx2Client\n"
        "first = httpx.AsyncClient()\nsecond = httpx2.AsyncClient()\n"
        'HttpxClient("http://example.test", http_client=first)\n'
        'Httpx2Client("http://example.test", http_client=second)\n'
        'HttpxClient("http://example.test", http_client=second)\n'
        'Httpx2Client("http://example.test", http_client=first)\n',
        encoding="utf-8",
    )
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": ["consumer.py"],
                "typeCheckingMode": "strict",
                "pythonVersion": "3.12",
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--project",
            str(config),
            "--pythonpath",
            sys.executable,
            "--outputjson",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    assert {
        (error["range"]["start"]["line"], error.get("rule")) for error in diagnostics
    } == {(8, "reportArgumentType"), (9, "reportArgumentType")}, diagnostics


async def test_native_client_types_timeout_headers_models_and_ownership(http_backend):
    http, sdk = http_backend
    requests = []

    def handler(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 0.25
        assert request.headers["Authorization"] == "Bearer override"
        return http.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content='data: {"type":"run.completed","run_id":"ok"}\n\n',
        )

    async with http.AsyncClient(
        transport=http.MockTransport(handler), timeout=0.25
    ) as native:
        async with sdk.Client("http://example.test", http_client=native) as client:
            events = [
                event
                async for event in client.agent_runs.stream_prompt(
                    run_id="a b",
                    body=sdk.models.RunRequest(prompt="Hello"),
                    options=sdk.RequestOptions(
                        headers={"Authorization": "Bearer override"}
                    ),
                )
            ]
            assert events[0].type is sdk.RunEventType.RUN_COMPLETED
            assert events[0].run_id == "ok"
            assert requests[0].content == b'{"prompt":"Hello"}'
            assert requests[0].url.raw_path == b"/v1/runs/a%20b/events"
        assert not native.is_closed
    async with sdk.Client("http://example.test") as owned:
        native = owned._transport.http_client
        assert isinstance(native, http.AsyncClient)
    assert native.is_closed


@pytest.mark.parametrize(
    "case",
    [
        "http_error",
        "broken_error",
        "read_error",
        "connect_error",
        "utf8",
        "schema",
        "media",
    ],
)
async def test_backend_native_failures_and_response_cleanup(http_backend, case):
    http, sdk = http_backend
    chunks = [b'data: {"type":"run.completed","run_id":"ok"}\n\n']
    if case == "utf8":
        chunks = [b"\xff"]
    elif case == "schema":
        chunks = [b'data: {"type":"unknown"}\n\n']
    elif case in {"http_error", "broken_error"}:
        chunks = [b'{"detail":"denied"}']
    source = _chunks(http, chunks, fail=case in {"read_error", "broken_error"})

    def handler(request):
        if case == "connect_error":
            raise http.ConnectError("offline", request=request)
        return http.Response(
            503 if case in {"http_error", "broken_error"} else 200,
            headers={
                "Content-Type": "text/html" if case == "media" else "text/event-stream"
            },
            stream=source,
        )

    async with (
        http.AsyncClient(transport=http.MockTransport(handler)) as native,
        sdk.Client("http://example.test", http_client=native) as client,
    ):
        expected = (
            sdk.HttpError
            if case in {"http_error", "broken_error"}
            else sdk.StreamParseError
            if case == "schema"
            else sdk.TransportError
        )
        with pytest.raises(expected) as raised:
            _ = [
                event async for event in client.agent_runs.stream_run_events(run_id="x")
            ]
        if case in {"http_error", "broken_error"}:
            assert raised.value.status == 503
            assert isinstance(raised.value.response, http.Response)
            assert raised.value.details == {"detail": "denied"}
        if case in {"read_error", "broken_error"}:
            assert isinstance(raised.value.__cause__, http.ReadError)
        if case == "connect_error":
            assert isinstance(raised.value.__cause__, http.ConnectError)
        else:
            assert source.closed


@pytest.fixture(params=["httpx", "httpx2"])
def http_backend(request, tmp_path):
    http = importlib.import_module(request.param)
    files = generate_python(build_ir(export_schema(app)), http_client=request.param)
    with load_package(tmp_path / "sdk", files) as sdk:
        yield http, sdk


def _chunks(http, chunks, *, fail=False):
    class Stream(http.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for chunk in chunks:
                yield chunk
            if fail:
                raise http.ReadError("disconnected")

        async def aclose(self):
            self.closed = True

    return Stream()
