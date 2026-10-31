import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcMessageType,
    RpcService,
    RpcSocket,
    RpcSocketProtocol,
)
from fastapi_canon.integrations.websockets.codegen import (
    generate_python_client,
    generate_typescript_client,
    render_python_client,
)
from fastapi_canon.integrations.websockets.codegen.bindings import (
    BindingContext,
    SocketBinding,
    bind_sockets,
    load_binding,
)
from fastapi_canon.integrations.websockets.codegen.cli import main
from fastapi_canon.integrations.websockets.codegen.ir import (
    SocketDecl,
    SocketProtocolDecl,
    UnsupportedSchemaError,
    build_ir,
)
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)

from .test_socket_roundtrip import LoopbackSocket, UnusedTransport

PACKAGE = "echo_client"

echo = RpcChannel("echo")


@echo.socket(
    "session",
    protocol=RpcSocketProtocol("echo", version="1", schemas=("echo.json",)),
    messages=(RpcMessageType.TEXT,),
)
async def session(socket: Inject[RpcSocket]) -> None:
    async for message in socket:
        await socket.send(f"echo:{message}")


@echo.socket("raw")
async def raw(socket: Inject[RpcSocket]) -> None:
    async for message in socket:
        await socket.send(message)


service = RpcService()
service.socket("/echo/session", session)
service.socket("/echo/raw", raw)

PYTHON_SESSION = '''
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from ..sockets import Socket


class EchoSession:
    """Sends one text message and reads one reply."""

    def __init__(self, socket: Socket) -> None:
        self.socket = socket

    async def call(self, text: str) -> str | bytes:
        await self.socket.send(text)
        return await self.socket.receive()


@asynccontextmanager
async def open_echo(
    socket: AbstractAsyncContextManager[Socket],
) -> AsyncIterator[EchoSession]:
    async with socket as opened:
        yield EchoSession(opened)
'''

TYPESCRIPT_SESSION = """
import type { Socket } from "../sockets";

/** Sends one text message and reads one reply. */
export class EchoSession {
  constructor(readonly socket: Socket) {}

  async call(text: string): Promise<string> {
    await this.socket.send(text);
    return String(await this.socket.receive());
  }

  [Symbol.asyncDispose](): Promise<void> {
    return this.socket.close();
  }
}

export async function openEcho(socket: Socket): Promise<EchoSession> {
  return new EchoSession(socket);
}
"""


@dataclass
class EchoBinding:
    protocol: str = "echo"
    contexts: list[BindingContext] = field(default_factory=list)

    def bind(self, context: BindingContext) -> SocketBinding:
        self.contexts.append(context)
        if context.language == "python":
            return SocketBinding("echo", PYTHON_SESSION, "open_echo", "EchoSession")
        return SocketBinding("echo", TYPESCRIPT_SESSION, "openEcho", "EchoSession")


BINDING = EchoBinding()


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return service.contract(title="Echo", base_url="ws://localhost").to_openrpc()


@pytest.fixture(scope="module")
def client_package(
    document: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ModuleType]:
    root = tmp_path_factory.mktemp("bound_client")
    generate_python_client(
        document,
        root / PACKAGE,
        PythonClientOptions(
            package=PACKAGE, client_name="EchoClient", bindings=(EchoBinding(),)
        ),
    )
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    try:
        yield importlib.import_module(PACKAGE)
    finally:
        sys.path.remove(str(root))
        for name in [
            name
            for name in sys.modules
            if name == PACKAGE or name.startswith(f"{PACKAGE}.")
        ]:
            del sys.modules[name]


def _client(package: ModuleType) -> Any:
    async def opener(endpoint: Any) -> LoopbackSocket:
        return LoopbackSocket(package, urlsplit(endpoint.url).path, service)

    return package.EchoClient.with_transports(UnusedTransport(), socket_opener=opener)


async def test_bound_sockets_return_the_binding_session(
    client_package: ModuleType,
) -> None:
    client = _client(client_package)
    bound = importlib.import_module(f"{PACKAGE}.bindings.echo")

    async with client.echo.session() as echo_session:
        assert isinstance(echo_session, bound.EchoSession)
        assert await echo_session.call("hi") == "echo:hi"
        assert echo_session.socket.endpoint.protocol == client_package.SocketProtocol(
            name="echo", version="1", schemas=("echo.json",)
        )


async def test_sockets_without_a_bound_protocol_stay_generic(
    client_package: ModuleType,
) -> None:
    client = _client(client_package)

    async with client.echo.raw() as socket:
        assert isinstance(socket, client_package.Socket)
        await socket.send(b"\x00")
        assert await socket.receive() == b"\x00"


def test_unbound_protocols_generate_a_generic_socket(document: dict[str, Any]) -> None:
    files = render_python_client(document, PythonClientOptions(package=PACKAGE))

    assert not any(name.startswith("bindings/") for name in files)
    assert "AbstractAsyncContextManager[Socket]" in files["namespaces/echo.py"]


def test_bindings_see_the_declared_protocols_and_resolved_schemas(
    document: dict[str, Any], tmp_path: Path
) -> None:
    binding = EchoBinding()
    contract = tmp_path / "contracts" / "echo.openrpc.json"
    options = PythonClientOptions(
        package=PACKAGE, bindings=(binding,), contract=contract
    )

    files = render_python_client(document, options)

    (context,) = binding.contexts
    assert context.language == "python"
    assert context.protocols == (
        SocketProtocolDecl(name="echo", version="1", schemas=("echo.json",)),
    )
    assert context.schemas() == ((tmp_path / "contracts" / "echo.json").as_uri(),)
    assert "class EchoSession" in files["bindings/echo.py"]
    assert files["bindings/__init__.py"].strip()  # the generated header only


def _sockets(*protocols: str | None) -> list[SocketDecl]:
    ir = build_ir(
        {
            "openrpc": "1.3.2",
            "info": {"title": "T", "version": "1"},
            "methods": [],
            "x-rpckit-sockets": [
                {
                    "name": f"s{index}",
                    "url": f"ws://localhost/s{index}",
                    "messageTypes": ["text"],
                    **(
                        {"x-rpckit-protocol": {"name": name}}
                        if name is not None
                        else {}
                    ),
                }
                for index, name in enumerate(protocols)
            ],
        }
    )
    return list(ir.sockets)


@dataclass
class StaticBinding:
    protocol: str
    result: object

    def bind(self, context: BindingContext) -> Any:
        return self.result


def _result(module: str = "m", open: str = "open_m", session: str = "M") -> Any:
    return SocketBinding(module, "", open, session)


@pytest.mark.parametrize("api_root", [None, "echo"])
async def test_python_bindings_can_export_identical_names(
    document: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_root: str | None,
) -> None:
    deployed = json.loads(json.dumps(document))
    deployed["x-rpckit-sockets"][1]["x-rpckit-protocol"] = {"name": "other"}
    package_name = "collision_client"
    generate_python_client(
        deployed,
        tmp_path / package_name,
        PythonClientOptions(
            package=package_name,
            client_name="EchoClient",
            api_root=api_root,
            bindings=(
                EchoBinding(),
                StaticBinding(
                    "other",
                    SocketBinding("other", PYTHON_SESSION, "open_echo", "EchoSession"),
                ),
            ),
        ),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        package = importlib.import_module(package_name)
        client = _client(package)
        namespace = client.echo if api_root is None else client
        async with namespace.session() as first, namespace.raw() as second:
            assert type(first).__module__ == f"{package_name}.bindings.echo"
            assert type(second).__module__ == f"{package_name}.bindings.other"
            assert await first.call("hi") == "echo:hi"
            assert await second.call("hi") == "hi"
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                del sys.modules[name]


@pytest.mark.parametrize(
    ("bindings", "message"),
    [
        (
            [StaticBinding("a", _result()), StaticBinding("a", _result("n"))],
            "Duplicate socket binding for 'a'",
        ),
        ([StaticBinding("c", _result())], "matches no socket protocol"),
        ([StaticBinding("a", object())], "must return a SocketBinding"),
        ([StaticBinding("a", _result(module="not-valid"))], "must be identifiers"),
        ([StaticBinding("a", _result(open="M"))], "open and session must differ"),
        (
            [StaticBinding("a", _result()), StaticBinding("b", _result())],
            "share the module name 'm'",
        ),
    ],
)
def test_invalid_bindings_are_rejected(bindings: list[Any], message: str) -> None:
    with pytest.raises(UnsupportedSchemaError, match=message):
        bind_sockets(_sockets("a", "b", None), bindings, "python")


def test_each_protocol_is_bound_once_with_its_distinct_declarations() -> None:
    binding = EchoBinding(protocol="a")

    bound = bind_sockets(_sockets("a", "a", None), [binding], "typescript")

    assert list(bound) == ["a"]
    (context,) = binding.contexts
    assert context.protocols == (SocketProtocolDecl(name="a"),)


def test_bindings_load_from_module_references() -> None:
    assert load_binding("tests.codegen.test_bindings:BINDING") is BINDING


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("tests.codegen.test_bindings:MISSING", "Cannot load socket binding"),
        ("tests.codegen.missing_module:BINDING", "Cannot load socket binding"),
        ("json:dumps", "is not a ProtocolBinding"),
        ("not-registered", "Unknown socket binding"),
    ],
)
def test_unloadable_bindings_are_reported(reference: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_binding(reference)


def test_the_cli_loads_bindings_next_to_the_config(
    document: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "echo.openrpc.json").write_text(json.dumps(document), "utf-8")
    (tmp_path / "local_echo_binding.py").write_text(
        dedent(
            f"""
            from fastapi_canon.integrations.websockets.codegen.bindings import (
                SocketBinding,
            )


            class Binding:
                protocol = "echo"

                def bind(self, context):
                    return SocketBinding(
                        "echo", {PYTHON_SESSION!r}, "open_echo", "EchoSession"
                    )


            BINDING = Binding()
            """
        ),
        "utf-8",
    )
    config = tmp_path / "rpcgen.toml"
    config.write_text(
        "version = 1\n\n"
        "[[clients]]\n"
        'schema = "echo.openrpc.json"\n'
        'language = "python"\n'
        'output = "echo_client"\n'
        'bindings = ["local_echo_binding:BINDING"]\n',
        "utf-8",
    )

    try:
        assert main(["generate", "--config", str(config)]) == 0
        assert main(["generate", "--config", str(config), "--check"]) == 0
    finally:
        sys.modules.pop("local_echo_binding", None)
    namespace = (tmp_path / "echo_client" / "namespaces" / "echo.py").read_text()
    assert "AbstractAsyncContextManager[_socket_binding_echo.EchoSession]" in namespace
    assert (tmp_path / "echo_client" / "bindings" / "echo.py").exists()
    assert str(tmp_path) not in sys.path


def test_the_cli_reports_unknown_bindings(
    document: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = tmp_path / "echo.openrpc.json"
    schema.write_text(json.dumps(document), "utf-8")

    code = main(
        [
            "generate",
            str(schema),
            "--language",
            "typescript",
            "--output",
            str(tmp_path / "generated"),
            "--binding",
            "not-registered",
        ]
    )

    assert code == 2
    assert "Unknown socket binding 'not-registered'" in capsys.readouterr().err


_RUNNER = """
import { EchoClient, Socket } from "./generated";
import { EchoSession } from "./generated/bindings/echo";
import { EchoSession as OtherSession } from "./generated/bindings/other";
import { RpcClientCore } from "./generated/core";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// Answers like the echo service: bound sockets prefix every reply.
function transport(url: string) {
  const replies: string[] = [];
  return {
    async receive() {
      const reply = replies.shift();
      if (reply === undefined) throw new Error("nothing to receive");
      return reply;
    },
    async send(message: string | ArrayBuffer | ArrayBufferView) {
      const text = typeof message === "string" ? message : "binary";
      replies.push(url.endsWith("/session") ? `echo:${text}` : text);
    },
    async close() {},
  };
}

async function main(): Promise<void> {
  const client = EchoClient.withTransports(
    {
      request: async () => undefined,
      notifications: async function* () {},
      close: async () => undefined,
    },
    { socketOpener: async (endpoint) => transport(String(endpoint.url)) },
  );

  {
    await using session = await client.echo.session();
    assert(session instanceof EchoSession, "bound sockets open the session");
    assert((await session.call("hi")) === "echo:hi", "session round trip");
    assert(session.socket.endpoint.protocol?.version === "1", "protocol");
  }

  const generic = await client.echo.raw();
  assert(generic instanceof Socket, "unbound sockets stay generic");
  await generic.close();

  await using other = await client.echo.other();
  assert(other instanceof OtherSession, "bindings may export the same names");
  assert(!(other instanceof EchoSession), "each socket keeps its own binding");

  for (const asynchronous of [false, true]) {
    for (const closeFails of [false, true]) {
      const failure = new Error("binding initialization failed");
      let closes = 0;
      const rpc = new RpcClientCore(
        {
          request: async () => undefined,
          notifications: async function* () {},
          close: async () => undefined,
        },
        {
          socketOpener: async () => ({
            ...transport("/session"),
            async close() {
              closes++;
              if (closeFails) throw new Error("cleanup failed");
            },
          }),
        },
      );
      const endpoint = {
        name: "echo.session" as const,
        url: "ws://localhost/session",
        messageTypes: ["text"] as const,
      };
      const initialize = asynchronous
        ? async () => { throw failure; }
        : () => { throw failure; };
      let caught: unknown;
      try {
        await rpc.openSocket(endpoint, initialize);
      } catch (error) {
        caught = error;
      }
      assert(caught === failure, "preserve the binding initialization error");
      assert(closes === 1, "close a transport whose binding failed to initialize");
    }
  }
}

void main();
"""


def _tools() -> tuple[str, str]:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    return npx, node


def test_bound_typescript_sockets_return_the_binding_session(
    document: dict[str, Any], tmp_path: Path
) -> None:
    npx, node = _tools()
    output = tmp_path / "generated"
    deployed = {
        **document,
        "x-rpckit-sockets": [
            *document["x-rpckit-sockets"],
            {
                "name": "echo.other",
                "url": "ws://localhost/other",
                "messageTypes": ["text"],
                "x-rpckit-protocol": {"name": "other"},
            },
        ],
        "servers": [
            {
                "name": "primary",
                "url": "ws://localhost/rpc",
                "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
            }
        ],
    }
    generate_typescript_client(
        deployed,
        output,
        TypeScriptClientOptions(
            client_name="EchoClient",
            with_transport="websocket",
            bindings=(
                EchoBinding(),
                StaticBinding(
                    "other",
                    SocketBinding(
                        "other", TYPESCRIPT_SESSION, "openEcho", "EchoSession"
                    ),
                ),
            ),
        ),
    )
    formatting = subprocess.run(
        [npx, "prettier", "--check", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert formatting.returncode == 0, formatting.stdout + formatting.stderr
    namespace = (output / "namespaces" / "echo.ts").read_text("utf-8")
    assert 'import * as socketBinding_echo from "../bindings/echo";' in namespace
    assert "session(): Promise<socketBinding_echo.EchoSession>" in namespace

    runner = tmp_path / "bound.test.ts"
    runner.write_text(dedent(_RUNNER), encoding="utf-8", newline="\n")
    compiled = tmp_path / "compiled"
    type_check = subprocess.run(
        [
            npx,
            "--yes",
            "--package",
            "typescript",
            "tsc",
            "--strict",
            "--target",
            "ES2022",
            "--module",
            "Node16",
            "--moduleResolution",
            "Node16",
            "--lib",
            "ES2022,DOM,ESNext.Disposable",
            "--skipLibCheck",
            "--outDir",
            str(compiled),
            str(runner),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert type_check.returncode == 0, type_check.stdout + type_check.stderr

    behavior = subprocess.run(
        [node, str(compiled / "bound.test.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert behavior.returncode == 0, behavior.stdout + behavior.stderr
