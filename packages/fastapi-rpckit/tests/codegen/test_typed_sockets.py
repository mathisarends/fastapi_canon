import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import pytest
from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcService,
    RpcSocketProtocol,
)
from fastapi_canon.integrations.websockets.codegen import (
    PythonClientOptions,
    TypeScriptClientOptions,
    generate_python_client,
    generate_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.bindings import bind_sockets
from fastapi_canon.integrations.websockets.codegen.ir import (
    UnsupportedSchemaError,
    build_ir,
)
from pydantic import Field

from ..test_typed_connections import session as typed_session
from ..test_typed_sockets import Ping, Pong
from .test_bindings import EchoBinding
from .test_bindings import session as raw_session
from .test_socket_roundtrip import LoopbackSocket, UnusedTransport

channel = RpcChannel("messages")


@channel.socket(protocol=RpcSocketProtocol("example"))
async def union(
    message: Annotated[Ping | Pong, Field(discriminator="type")],
) -> Annotated[Ping | Pong, Field(discriminator="type")]:
    return message


app = RpcService()
app.socket("/union", union)


@pytest.mark.parametrize("api_root", [None, "messages"])
async def test_python_union_messages_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_root: str | None,
) -> None:
    document = app.contract(title="Union", base_url="ws://h").to_openrpc()
    package_name = "union_client"
    generate_python_client(
        document,
        tmp_path / package_name,
        PythonClientOptions(package=package_name, api_root=api_root),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        package = importlib.import_module(package_name)

        async def opener(endpoint):
            return LoopbackSocket(package, urlsplit(endpoint.url).path, app)

        client = package.UnionClient.with_transports(
            UnusedTransport(), socket_opener=opener
        )
        namespace = client.messages if api_root is None else client
        async with namespace.union() as socket:
            await socket.send(package.Ping(value=3))
            result = await socket.receive()
            assert isinstance(result, package.Ping)
            assert result.value == 3
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                del sys.modules[name]


async def test_raw_protocol_binding_and_typed_connection_share_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mixed = RpcService()
    mixed.socket("/typed", typed_session)
    mixed.socket("/echo", raw_session)
    document = mixed.contract(title="Mixed", base_url="ws://h").to_openrpc()
    package_name = "mixed_client"
    generate_python_client(
        document,
        tmp_path / package_name,
        PythonClientOptions(package=package_name, bindings=(EchoBinding(),)),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        package = importlib.import_module(package_name)

        async def opener(endpoint):
            return LoopbackSocket(package, urlsplit(endpoint.url).path, mixed)

        client = package.MixedClient.with_transports(
            UnusedTransport(), socket_opener=opener
        )
        async with client.echo.session() as bound:
            assert await bound.call("not JSON") == "echo:not JSON"
        async with client.sessions.session() as socket:
            assert isinstance(await socket.receive(), package.Ready)
            await socket.send(package.Ping(value=41))
            assert (await socket.receive()).value == 41
            assert (await socket.receive()).value == 42
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                del sys.modules[name]


def test_foreign_bindings_do_not_wrap_already_decoded_messages() -> None:
    class Binding:
        protocol = "example"

        def bind(self, context):
            raise AssertionError("Typed sockets must not invoke the binding")

    document = app.contract(title="Union", base_url="ws://h").to_openrpc()
    with pytest.raises(UnsupportedSchemaError, match="typed JSON"):
        bind_sockets(iter(build_ir(document).sockets), [Binding()], "python")


@pytest.mark.parametrize("messages", [{}, {"client": {}}, {"client": {}, "server": 1}])
def test_malformed_directional_contracts_are_rejected(messages) -> None:
    document = app.contract(title="Union", base_url="ws://h").to_openrpc()
    document["x-rpckit-sockets"][0]["messages"] = messages
    with pytest.raises(UnsupportedSchemaError, match="client/server"):
        build_ir(document)


@pytest.mark.parametrize("api_root", [None, "messages"])
def test_union_clients_pass_target_formatting_and_types(
    tmp_path: Path,
    api_root: str | None,
) -> None:
    document = app.contract(title="Union", base_url="ws://h").to_openrpc()
    python_root = tmp_path / "python_client"
    generate_python_client(
        document,
        python_root,
        PythonClientOptions(package="python_client", api_root=api_root),
    )
    for command in ("check", "format"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                command,
                *(["--check"] if command == "format" else []),
                str(python_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx is None:
        pytest.skip("npx is required")
    target = tmp_path / "typescript"
    document["servers"] = [
        {
            "name": "rpc",
            "url": "ws://h/rpc",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    generate_typescript_client(
        document,
        target,
        TypeScriptClientOptions(api_root=api_root, with_transport="websocket"),
    )
    result = subprocess.run(
        [npx, "prettier", "--check", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
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
            "ESNext",
            "--moduleResolution",
            "Bundler",
            "--lib",
            "ES2022,DOM,ESNext.Disposable",
            "--skipLibCheck",
            "--noEmit",
            str(target / "index.ts"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
