# Socket protocol bindings

A socket forwards an application protocol unchanged. This example declares a
small one and plugs typed clients for it into both generators. It stands in for
larger protocols such as the Chrome DevTools Protocol (CDP), which work the same
way.

`calc` is shaped like CDP: each command carries an `id`, and its reply repeats
that id, so several commands can be in flight at once.

```text
-> {"id": 1, "method": "add", "params": [2, 3]}
<- {"id": 1, "result": 5}
```

| File | Role |
|---|---|
| [`calculator.py`](calculator.py) | The service: a JSON-RPC method and a socket declared with `protocol=CALC`; exports the contract and a FastAPI app |
| [`calc_binding.py`](calc_binding.py) | The binding: Python and TypeScript sources of `CalcSession`, selected by the protocol name |
| [`rpcgen.toml`](rpcgen.toml) | Exports the contract and generates both clients with `bindings = ["calc_binding:BINDING"]` |
| [`calculator.openrpc.json`](calculator.openrpc.json) | The contract; the socket carries `x-rpckit-protocol` |
| [`python/calc_client`](python/calc_client) and [`typescript`](typescript) | Generated clients; `bindings/calc` holds the session |

`calculator.session()` returns a `CalcSession` rather than a generic socket:

```python
async with CalculatorClient.connect(host="localhost:8000") as client:
    async with client.calculator.session() as calc:
        total, product = await asyncio.gather(calc.add(2, 3), calc.multiply(4, 5))
```

```ts
await using client = await CalculatorClient.connect({ host: "localhost:8000" });
await using calc = await client.calculator.session();
const [total, product] = await Promise.all([calc.add(2, 3), calc.multiply(4, 5)]);
```

Without the binding, both generators emit the same method returning the generic
`Socket`, with the declaration available as `endpoint.protocol`.

## Applying it to CDP

A CDP service declares `RpcSocketProtocol(name="cdp", ...)` and forwards
messages to the browser instead of answering them. Its binding keeps this
example's shape:

- Python: `open` wraps an existing CDP client around the socket, for example
  a cdpify `Client` with a transport that sends and receives through `Socket`.
- TypeScript: the session types `send(method, params)` and `on(event)` with
  `ProtocolMapping` from the `devtools-protocol` npm package, which the
  generated client then needs as a dependency.

Publish such a binding as a package with a `fastapi_canon.integrations.websockets.socket_bindings`
entry point to select it by name, for example `bindings = ["cdp"]`.

## Run it

```bash
uv run uvicorn examples.socket_protocol.calculator:app
uv run python -m fastapi_canon.integrations.websockets.codegen.cli generate \
  --config examples/socket_protocol/rpcgen.toml
```

[`tests/test_socket_protocol_example.py`](../../tests/test_socket_protocol_example.py)
serves the app with uvicorn and drives it with both generated clients.
