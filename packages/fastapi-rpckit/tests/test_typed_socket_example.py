import asyncio
import importlib
import json
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn

from examples.typed_socket.ping import app

EXAMPLE = Path(__file__).parents[1] / "examples" / "typed_socket"


@pytest.fixture
async def host() -> AsyncIterator[str]:
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            ready.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = Server(uvicorn.Config(app, ws="websockets-sansio", log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        await asyncio.wait_for(ready.wait(), 10)
        try:
            yield f"127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await serving


@pytest.fixture
def ping_client() -> Iterator[ModuleType]:
    sys.path.insert(0, str(EXAMPLE / "python"))
    try:
        yield importlib.import_module("ping_client")
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name == "ping_client" or name.startswith("ping_client."):
                del sys.modules[name]


async def test_python_typed_socket_round_trip(
    host: str, ping_client: ModuleType
) -> None:
    async with ping_client.TypedSocketClient.connect(host=host) as client:
        assert await client.messages.health()
        async with client.messages.ping() as socket:
            with pytest.raises(ValueError, match="outgoing"):
                await socket.send(ping_client.Pong(value=0))
            await socket.send(ping_client.Ping(value=41))
            response = await socket.receive()
            assert isinstance(response, ping_client.Pong)
            assert response.value == 42
        async with client.messages.session() as socket:
            assert (await socket.receive()).value == 0
            await socket.send(ping_client.Ping(value=41))
            assert (await socket.receive()).value == 41
            assert (await socket.receive()).value == 42


RUNNER = """
import { TypedSocketClient, RpcStreamFailed } from "./generated";

async function main(host: string): Promise<void> {
  await using client = await TypedSocketClient.connect({ host });
  if (!(await client.messages.health())) throw new Error("health failed");
  await using socket = await client.messages.ping();
  await socket.send({ type: "ping", value: 41 });
  const response = await socket.receive();
  const value: number = response.value;
  if (response.type !== "pong" || value !== 42) throw new Error("wrong response");

  await using session = await client.messages.session();
  if ((await session.receive()).value !== 0) throw new Error("missing initial event");
  await session.send({ type: "ping", value: 41 });
  if ((await session.receive()).value !== 41) throw new Error("missing first response");
  if ((await session.receive()).value !== 42) throw new Error("missing response");

  if (false) {
    // @ts-expect-error The input is Ping, not Pong.
    await socket.send({ type: "pong", value: 1 });
    // @ts-expect-error The value is a number.
    await socket.send({ type: "ping", value: "1" });
    // @ts-expect-error The response is Pong.
    const type: "ping" = response.type;
  }

  await using invalid = await client.messages.ping();
  await invalid.send({ type: "ping", value: "secret" } as never);
  try {
    await invalid.receive();
    throw new Error("invalid input should close the connection");
  } catch (error) {
    if (!(error instanceof RpcStreamFailed) || error.code !== 1002) throw error;
  }
}

void main("__HOST__");
"""


def test_python_sdk_preserves_message_types(tmp_path: Path) -> None:
    source = tmp_path / "consumer.py"
    source.write_text(
        """
from typing import assert_type
from ping_client import Ping, Pong, TypedSocketClient

async def consume(client: TypedSocketClient) -> None:
    async with client.messages.ping() as socket:
        assert_type(await socket.receive(), Pong)
        await socket.send(Ping(value=41))
        await socket.send(Pong(value=41))
""",
        encoding="utf-8",
    )
    config = tmp_path / "pyrightconfig.json"
    config.write_text(json.dumps({"extraPaths": [str(EXAMPLE / "python")]}), "utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.12",
            "--project",
            str(config),
            "--outputjson",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    errors = [item for item in diagnostics if item["severity"] == "error"]
    assert len(errors) == 1, diagnostics
    assert errors[0]["rule"] == "reportArgumentType"
    assert "Pong" in errors[0]["message"] and "Ping" in errors[0]["message"]


async def test_typescript_typed_socket_round_trip(host: str, tmp_path: Path) -> None:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    shutil.copytree(EXAMPLE / "typescript", tmp_path / "generated")
    runner = tmp_path / "runner.ts"
    runner.write_text(RUNNER.replace("__HOST__", host), encoding="utf-8")
    compiled = tmp_path / "compiled"
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
    assert result.returncode == 0, result.stdout + result.stderr
    behavior = await asyncio.create_subprocess_exec(
        node,
        str(compiled / "runner.js"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(behavior.communicate(), 30)
    assert behavior.returncode == 0, output.decode()
