# Examples

These examples show the fastapi_canon.integrations.websockets API directly, without a web framework, generated
client, or application structure. Each file is standalone and executable.

- [`basic.py`](basic.py) declares a typed method, serves one raw JSON-RPC request,
  and prints the response.
- [`features.py`](features.py) mounts multiple channels on one versioned
  service.
- [`notifications.py`](notifications.py) decorates an injected async event
  source and declares its typed payload.
- [`schemas.py`](schemas.py) renders JSON Schema and OpenRPC documents from the
  same protocol definition.
- [`generated_clients`](generated_clients) contains inspectable Python and
  TypeScript clients generated from
  [`automation.py`](generated_clients/automation.py). That module declares
  server methods, an event, binary streams, and a raw socket, and exports the
  contract that becomes
  [`automation.openrpc.json`](generated_clients/automation.openrpc.json). The
  binary streams generate
  [`streams.py`](generated_clients/python/automation_client/streams.py) and
  [`streams.ts`](generated_clients/typescript/streams.ts). Regenerate the
  contract and both clients from this directory's
  [`rpcgen.toml`](generated_clients/rpcgen.toml):

  ```bash
  uv run python -m fastapi_canon.integrations.websockets.codegen.cli generate \
    --config examples/generated_clients/rpcgen.toml
  ```
- [`socket_protocol`](socket_protocol/README.md) declares a small CDP-shaped
  protocol on a socket and plugs typed Python and TypeScript clients for it
  into the generators.
- [`typed_socket`](typed_socket/README.md) declares an annotated message handler,
  validates incoming and outgoing JSON with Pydantic, and includes generated
  Python and TypeScript clients exercised against a live FastAPI server.

Run an example from the repository root:

```bash
uv run python examples/basic.py
uv run python examples/features.py
uv run python examples/notifications.py
uv run python examples/schemas.py
```
