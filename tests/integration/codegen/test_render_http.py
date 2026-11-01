import asyncio
import importlib
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKENDS = ("httpx", "httpx2")


@pytest.fixture(scope="module")
def generated_http_sdk(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    directory = tmp_path_factory.mktemp("generated-http")
    (directory / "openapi.json").write_text(json.dumps(_document()), encoding="utf-8")
    config = [
        "[codegen]\nversion = 1",
        '[codegen.sources.api]\nkind = "openapi"\ninput = "openapi.json"',
    ]
    for backend in BACKENDS:
        config.append(
            f'[codegen.targets.{backend}]\nlanguage = "python"\nsources = ["api"]\n'
            f'output = "{backend}_sdk"\npackage = "{backend}_sdk"\n'
            f'http-client = "{backend}"'
        )
    config.append(
        '[codegen.targets.browser]\nlanguage = "typescript"\nsources = ["api"]\n'
        'output = "browser"'
    )
    (directory / "fastapi-canon.toml").write_text("\n\n".join(config), encoding="utf-8")
    generated = subprocess.run(
        [sys.executable, "-m", "fastapi_canon", "generate"],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert (directory / "httpx_sdk/__init__.py").is_file()
    assert (directory / "httpx2_sdk/__init__.py").is_file()
    assert (directory / "browser/index.ts").is_file()
    sys.path.insert(0, str(directory))
    try:
        yield directory
    finally:
        sys.path.remove(str(directory))
        for name in tuple(sys.modules):
            if name.startswith(("httpx_sdk", "httpx2_sdk")):
                del sys.modules[name]


@pytest.fixture(scope="module")
def browser_http_sdk(generated_http_sdk: Path) -> tuple[str, Path]:
    node = shutil.which("node")
    compiler = ROOT / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
    if node is None or not compiler.is_file():
        pytest.skip("Run npm ci --ignore-scripts --prefix packages/fastapi-streams")
    usage = generated_http_sdk / "usage.ts"
    usage.write_text(
        'import { ApiClient } from "./browser/index.js";\n'
        "export function nullable(client: ApiClient) {\n"
        "  return client.records.optional({tag: null, xNote: null});\n}\n",
        encoding="utf-8",
    )
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
            "--outDir",
            str(generated_http_sdk / "js"),
            str(usage),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    runner = generated_http_sdk / "runner.cjs"
    runner.write_text(
        """
const {createClient, TransportError, ProtocolError, ProblemError}
  = require("./js/browser/index.js");
const client = createClient({baseUrl: process.argv[2]});
const operation = process.argv[3];
const mode = process.argv[4];
const params = operation === "optional" ? {tag: null, xNote: null}
  : operation === "required" ? {tag: null} : {};
(async () => {
  try {
    const value = await client.records[operation](params, {
      headers: {"X-Wire-Mode": mode},
      ...(operation === "delayed" ? {timeoutMs: 30} : {}),
    });
    console.log(JSON.stringify({value: value === undefined ? "void" : value}));
  } catch (error) {
    console.log(JSON.stringify({error: error.name,
      transport: error instanceof TransportError,
      protocol: error instanceof ProtocolError, problem: error instanceof ProblemError,
      cause: error.cause?.name}));
  } finally { await client.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
        encoding="utf-8",
    )
    return node, runner


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("operation", "mode"),
    [
        ("json_null", "empty"),
        ("bodyless", "unexpected"),
        ("mixed", "wrong"),
        ("mixed", "created-wrong"),
        ("wildcard", "fallback-wrong"),
        ("malformed", "normal"),
    ],
)
async def test_generated_python_http_rejects_invalid_responses(
    generated_http_sdk: Path, http_server: str, backend: str, operation: str, mode: str
) -> None:
    sdk: Any = importlib.import_module(f"{backend}_sdk")
    async with asyncio.timeout(10), sdk.ApiClient(http_server) as client:
        with pytest.raises(sdk.ProtocolError):
            await getattr(client.records, operation)(
                options=sdk.RequestOptions(headers={"X-Wire-Mode": mode}, timeout=3)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("operation", "mode", "expected"),
    [
        ("json_null", "normal", None),
        ("bodyless", "normal", None),
        ("mixed", "normal", "ok"),
        ("mixed", "created", 12),
        ("wildcard", "normal", "ok"),
        ("wildcard", "fallback", None),
        ("wildcard_json", "normal", None),
        ("wildcard_json", "fallback", 12),
    ],
)
async def test_generated_python_http_selects_response_for_actual_status(
    generated_http_sdk: Path,
    http_server: str,
    backend: str,
    operation: str,
    mode: str,
    expected: object,
) -> None:
    sdk: Any = importlib.import_module(f"{backend}_sdk")
    async with asyncio.timeout(10), sdk.ApiClient(http_server) as client:
        result = await getattr(client.records, operation)(
            options=sdk.RequestOptions(headers={"X-Wire-Mode": mode}, timeout=3)
        )
        assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_generated_python_http_omits_optional_null_parameters(
    generated_http_sdk: Path, http_server: str, backend: str
) -> None:
    sdk: Any = importlib.import_module(f"{backend}_sdk")
    async with asyncio.timeout(10), sdk.ApiClient(http_server) as client:
        result = await client.records.optional(tag=None, x_note=None)
        assert result == {"query": "", "header": "missing"}
        with pytest.raises(ValueError, match="null"):
            await client.records.required(tag=None)


@pytest.mark.parametrize(
    ("operation", "mode", "expected"),
    [
        ("jsonNull", "normal", None),
        ("bodyless", "normal", "void"),
        ("wildcard", "normal", "ok"),
        ("wildcard", "fallback", "void"),
        ("wildcardJson", "normal", "void"),
        ("wildcardJson", "fallback", 12),
        ("optional", "normal", {"query": "", "header": "missing"}),
    ],
)
def test_generated_typescript_http_serializes_and_selects_supported_contracts(
    browser_http_sdk: tuple[str, Path],
    http_server: str,
    operation: str,
    mode: str,
    expected: object,
) -> None:
    outcome = _typescript_call(browser_http_sdk, http_server, operation, mode)
    assert outcome == {"value": expected}


@pytest.mark.parametrize(
    ("operation", "mode", "error", "transport", "protocol", "problem"),
    [
        ("jsonNull", "empty", "ProtocolError", True, True, False),
        ("bodyless", "unexpected", "ProtocolError", True, True, False),
        ("wildcard", "fallback-wrong", "ProtocolError", True, True, False),
        ("malformed", "normal", "ProtocolError", True, True, False),
        ("broken", "normal", "TransportError", True, False, False),
        ("problemBroken", "normal", "TransportError", True, False, False),
        ("required", "normal", "TypeError", False, False, False),
        ("problem", "normal", "ProblemError", False, False, True),
        ("delayed", "normal", "AbortError", False, False, False),
    ],
)
def test_generated_typescript_http_preserves_error_and_cancellation_contracts(
    browser_http_sdk: tuple[str, Path],
    http_server: str,
    operation: str,
    mode: str,
    error: str,
    transport: bool,
    protocol: bool,
    problem: bool,
) -> None:
    outcome = _typescript_call(browser_http_sdk, http_server, operation, mode)
    assert outcome["error"] == error
    assert outcome["transport"] is transport
    assert outcome["protocol"] is protocol
    assert outcome["problem"] is problem
    if operation in {"broken", "problemBroken"}:
        assert outcome["cause"] == "TypeError"


class _HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        mode = self.headers.get("X-Wire-Mode", "normal")
        status = 200
        media_type = "application/json"
        body = b'"ok"'
        if path == "/json-null":
            body = b"" if mode == "empty" else b"null"
        elif path == "/bodyless":
            body = b"null" if mode == "unexpected" else b""
        elif path == "/mixed":
            status = 201 if mode.startswith("created") else 200
            body = b"12" if mode in {"created", "wrong"} else b'"ok"'
        elif path == "/wildcard" and mode.startswith("fallback"):
            status = 202
            body = b"null" if mode == "fallback-wrong" else b""
        elif path == "/wildcard-json":
            status = 202 if mode == "fallback" else 200
            body = b"12" if mode == "fallback" else b""
        elif path in {"/optional", "/required"}:
            body = json.dumps(
                {
                    "query": urlsplit(self.path).query,
                    "header": self.headers.get("x-note", "missing"),
                }
            ).encode()
        elif path == "/malformed":
            body = b"["
        elif path == "/problem":
            status = 400
            media_type = "application/problem+json"
            body = json.dumps(
                {
                    "type": "urn:audit:problem",
                    "title": "Audit failure",
                    "code": "audit_problem",
                    "status": 400,
                }
            ).encode()
        elif path in {"/broken", "/problem-broken"}:
            body = b'"partial'
            if path == "/problem-broken":
                status = 400
                media_type = "application/problem+json"
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header(
            "Content-Length", str(len(body) + (10 if "broken" in path else 0))
        )
        self.end_headers()
        if path == "/delayed":
            self.wfile.flush()
            time.sleep(0.2)
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            # Cancellation intentionally closes the connection before the body.
            pass
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        pass


def _typescript_call(
    sdk: tuple[str, Path], base_url: str, operation: str, mode: str
) -> dict[str, Any]:
    node, runner = sdk
    completed = subprocess.run(
        [node, str(runner), base_url, operation, mode],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def _document() -> dict[str, Any]:
    def response(schema: dict[str, Any] | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {"description": "OK"}
        if schema is not None:
            value["content"] = {"application/json": {"schema": schema}}
        return value

    string = response({"type": "string"})
    mapping = response({"type": "object", "additionalProperties": {"type": "string"}})
    operations: dict[str, Any] = {
        "json-null": {"200": response({"type": "null"})},
        "bodyless": {"200": response()},
        "mixed": {"200": string, "201": response({"type": "integer"})},
        "wildcard": {"200": string, "2XX": response()},
        "wildcard-json": {"200": response(), "2XX": response({"type": "integer"})},
        "optional": {"200": mapping},
        "required": {"200": mapping},
        "malformed": {"200": string},
        "broken": {"200": string},
        "problem-broken": {"200": string},
        "problem": {"200": string},
        "delayed": {"200": string},
    }
    paths = {
        "/" + name: {
            "get": {
                "operationId": "records." + name.replace("-", "_"),
                "responses": responses,
            }
        }
        for name, responses in operations.items()
    }
    for name in ("optional", "required"):
        paths["/" + name]["get"]["parameters"] = [
            {
                "name": "tag",
                "in": "query",
                "required": name == "required",
                "schema": {"type": ["string", "null"]},
            },
            {
                "name": "x-note",
                "in": "header",
                "schema": {"type": ["string", "null"]},
            },
        ]
    return {
        "openapi": "3.1.0",
        "info": {"title": "HTTP", "version": "1"},
        "paths": paths,
    }
