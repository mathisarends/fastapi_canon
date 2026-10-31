# RPC successor parity and consumer migration

`fastapi-rpckit` is the FastAPI-first successor to pyrpckit. Its initial import
preserved the predecessor's runtime, schemas, templates and tests from commit
`853579eb08903bd95f8ce03f7608a3703ba40add` (pyrpckit 0.11.0), under the
`fastapi_canon.integrations.websockets` namespace. FastAPI is required. There is no `rpckit` import shim.

Existing `x-rpckit-*` extensions, `.rpcgen` manifests and `rpckit.*` telemetry
names are intentional compatibility points. Subsequent releases have source and
generated-client breaking changes, including the 0.3.0 socket API changes and
removal of server-to-client RPC. Follow the package [migration guide](../packages/fastapi-rpckit/docs/migration.md)
and [changelog](../packages/fastapi-rpckit/CHANGELOG.md) rather than assuming the
current API is identical to the imported predecessor.

## Regression scope

The suite covers methods, notifications, validation, typed errors, FastAPI
mounting and admission, Dishka scopes, subscriptions, binary/typed sockets,
deadlines, cancellation, recovery, shutdown, tracing and generated clients.
Installed-wheel checks exercise independent imports, CLI/client generation and
real routing. The composed stack also covers shared Dishka state and cleanup.

## Consumer contract checks

Cara's recorded contract snapshot came from gateway commit
`43178b931157caab9da59c1dad33cf8778af1470`, with 15 methods and SHA-256
`dee9bc02d414d978cfcdaf001b1ee1399c4dd9ed17b72773b3246e905391b004`.
Contract/SDK checks do not start or migrate the application.

Repeat against an exported consumer contract using an actual installed wheel.
From the workspace root, install the Streams TypeScript tooling first, build
Canon and replace `VERSION` with the built package version:

```console
npm ci --ignore-scripts --prefix packages/fastapi-streams
uv build --package fastapi-canon --out-dir dist/fastapi-canon
uv run --isolated --no-project --no-cache --with "dist/fastapi-canon/fastapi_canon-VERSION-py3-none-any.whl[codegen]" --with ruff python -I scripts/check_rpc_consumer.py PATH_TO_CONSUMER_OPENRPC_JSON
```

The script generates both SDKs, checks drift, imports/compiles Python, runs its
Ruff checks and compiles TypeScript strictly. `--no-cache` prevents reuse of an
older isolated install of a rebuilt development wheel with the same filename.

## Application migration boundary

1. Replace the dependency and `rpckit` imports with `fastapi-canon[websockets]` and
   `fastapi_canon.integrations.websockets`. Use package-root FastAPI APIs and
   `fastapi_canon.integrations.websockets.adapters.dishka` for Dishka integration. A mechanical
   namespace replacement can leave references to removed modules.
2. Apply all intervening changelog migrations for request IDs, middleware,
   transports, sockets and removed server-to-client RPC. Use the single Canon version and migrate feature imports as documented
   in the [package migration guide](../packages/fastapi-canon/docs/package-migration.md).
3. Regenerate committed SDKs and run the application's mounting, authentication,
   Dishka, subscription and voice/media tests.

Full Cara application migration remains open. SDK compilation alone cannot prove
runtime scope, authorization or media compatibility, and this repository does
not modify external consumers automatically.
