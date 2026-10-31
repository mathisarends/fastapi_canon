# Typed JSON socket

`ping.py` declares `async def ping(message: Ping) -> Pong`. The handler receives
one validated `Ping` and returns a `Pong`; the library owns the connection loop.
Malformed messages never reach the handler;
the transport adds no JSON-RPC envelope to these messages.

The same app has a small `health()` JSON-RPC method. As in the existing client
examples, that server supplies the connection configuration used by `connect()`.

The connection handler `session(socket: Inject[PingSocket])` infers the same JSON
validation from `type PingSocket = RpcSocket[Ping, Pong]`, with no second type
declaration. It sends an initial `Pong(value=0)` before receiving input and sends
two messages per `Ping`. This handler owns its loop and keeps one dependency scope
per connection. Both generated clients exercise this flow as well as the managed
message handler. Bare `RpcSocket` remains transparent text/binary transport.

```console
uv run --locked uvicorn examples.typed_socket.ping:app
uv run --locked python -m fastapi_canon.integrations.websockets.codegen.cli generate --config examples/typed_socket/rpcgen.toml
```

Python:

```python
from ping_client import Ping, TypedSocketClient

async with TypedSocketClient.connect(host="localhost:8000") as client:
    async with client.messages.ping() as socket:
        await socket.send(Ping(value=41))
        result = await socket.receive()
        assert result.value == 42
```

TypeScript:

```typescript
import { TypedSocketClient } from "./typescript";

await using client = await TypedSocketClient.connect({ host: "localhost:8000" });
await using socket = await client.messages.ping();
await socket.send({ type: "ping", value: 41 });
const result = await socket.receive();
console.log(result.value);
```

The SDK exposes `Socket<Pong, Ping>`: receive `Pong`, send `Ping`. TypeScript's
payload types are static; runtime Pydantic validation runs on the server. The
Python SDK also validates against its generated models. See the [socket guide](../../docs/sockets.md#typed-json-messages)
for validation, close codes, schema support, and protocol-binding boundaries.

`tests/test_typed_socket_example.py` compiles the TypeScript caller with negative
type checks and drives both clients against a live uvicorn server.
