"""A socket that speaks its own small protocol, next to a JSON-RPC channel.

``calc`` is shaped like the Chrome DevTools Protocol: every command carries an
``id`` and its reply repeats it, so several commands can be in flight at once::

    -> {"id": 1, "method": "add", "params": [2, 3]}
    <- {"id": 1, "result": 5}

Canon declares the protocol for the contract; the handler sees
the raw messages. A proxy, such as one forwarding CDP to a browser, would relay
them instead of answering.
"""

import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcMessageType,
    RpcService,
    RpcSocket,
    RpcSocketProtocol,
    ServerVariable,
    create_router,
)

CALC = RpcSocketProtocol(name="calc", version="1")

OPERATIONS: dict[str, Callable[[float, float], float]] = {
    "add": lambda a, b: a + b,
    "multiply": lambda a, b: a * b,
}

calculator = RpcChannel("calculator")


@calculator.method(summary="List the operations the calc protocol supports.")
async def operations() -> list[str]:
    return sorted(OPERATIONS)


@calculator.socket(
    protocol=CALC,
    messages=(RpcMessageType.TEXT,),
    summary="Evaluate calc commands.",
)
async def session(socket: Inject[RpcSocket]) -> None:
    async for message in socket:
        command = json.loads(message)
        operation = OPERATIONS.get(command["method"])
        if operation is None:
            reply = {"id": command["id"], "error": f"Unknown {command['method']!r}"}
        else:
            reply = {"id": command["id"], "result": operation(*command["params"])}
        await socket.send(json.dumps(reply))


service = RpcService()
service.socket("/calculator/rpc", channels=(calculator,))
service.socket("/calculator/session", session)

contract = service.contract(
    title="Calculator",
    base_url="ws://{host}",
    variables={"host": ServerVariable(default="localhost:8000")},
)

app = FastAPI()
app.include_router(create_router(service))
