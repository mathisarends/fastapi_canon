import json
import subprocess
import sys
from pathlib import Path

from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions


def test_generated_backends_and_differently_named_custom_factories_are_typed(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).parents[2] / "examples/generated_clients/automation.openrpc.json"
    )
    generate_python_client(
        json.loads(source.read_text("utf-8")),
        tmp_path / "factory_client",
        PythonClientOptions(package="factory_client", with_transport="websocket"),
    )
    consumer = tmp_path / "factories.py"
    consumer.write_text(
        """\
from collections.abc import Mapping
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Subprotocol
from factory_client.transport import WebSocketFactory
from factory_client.sockets import SocketConnectionFactory
from factory_client.streams import BinaryWebSocketFactory

async def custom(
    endpoint: str, *, subprotocols: list[str] | None,
    additional_headers: Mapping[str, str] | None,
) -> ClientConnection:
    protocols = [Subprotocol(value) for value in subprotocols] if subprotocols else None
    return await connect(endpoint, subprotocols=protocols,
                         additional_headers=additional_headers)

custom_rpc: WebSocketFactory = custom
custom_socket: SocketConnectionFactory = custom
custom_binary: BinaryWebSocketFactory = custom
""",
        encoding="utf-8",
    )
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.12",
            str(consumer),
            str(tmp_path / "factory_client/transport.py"),
            str(tmp_path / "factory_client/sockets.py"),
            str(tmp_path / "factory_client/streams.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
