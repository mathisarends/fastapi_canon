import asyncio
import importlib
import shutil
import socket as sockets
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn

from examples.socket_protocol.calculator import app

EXAMPLE = Path(__file__).parents[1] / "examples" / "socket_protocol"

_RUNNER = """
import { CalculatorClient } from "./generated";
import { CalcError, CalcSession } from "./generated/bindings/calc";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function main(host: string): Promise<void> {
  await using client = await CalculatorClient.connect({ host });
  const operations = await client.calculator.operations();
  assert(operations.join() === "add,multiply", `operations ${operations}`);

  await using calc = await client.calculator.session();
  assert(calc instanceof CalcSession, "the binding opens a CalcSession");
  const [sum, product] = await Promise.all([calc.add(2, 3), calc.multiply(4, 5)]);
  assert(sum === 5 && product === 20, `results ${sum} ${product}`);
  try {
    await calc.call("divide", 1, 0);
    throw new Error("divide should fail");
  } catch (error) {
    assert(error instanceof CalcError, `error ${String(error)}`);
  }
}

void main("__HOST__");
"""


@pytest.fixture
async def host() -> AsyncIterator[str]:
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets: list[sockets.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            ready.set()

    with sockets.socket() as listener:
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
def calc_client() -> Iterator[ModuleType]:
    root = str(EXAMPLE / "python")
    sys.path.insert(0, root)
    try:
        yield importlib.import_module("calc_client")
    finally:
        sys.path.remove(root)
        for name in [name for name in sys.modules if name.startswith("calc_client")]:
            del sys.modules[name]


async def test_the_python_binding_speaks_calc_over_a_real_socket(
    host: str, calc_client: ModuleType
) -> None:
    bound = importlib.import_module("calc_client.bindings.calc")

    async with calc_client.CalculatorClient.connect(host=host) as client:
        assert await client.calculator.operations() == ["add", "multiply"]
        async with client.calculator.session() as calc:
            assert isinstance(calc, bound.CalcSession)
            assert await asyncio.gather(calc.add(2, 3), calc.multiply(4, 5)) == [5, 20]
            with pytest.raises(bound.CalcError, match="divide"):
                await calc.call("divide", 1, 0)


async def test_the_typescript_binding_speaks_calc_over_a_real_socket(
    host: str, tmp_path: Path
) -> None:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    shutil.copytree(EXAMPLE / "typescript", tmp_path / "generated")
    runner = tmp_path / "calc.test.ts"
    runner.write_text(_RUNNER.replace("__HOST__", host), encoding="utf-8", newline="\n")
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

    behavior = await asyncio.create_subprocess_exec(
        node,
        str(compiled / "calc.test.js"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(behavior.communicate(), 60)
    assert behavior.returncode == 0, output.decode()
