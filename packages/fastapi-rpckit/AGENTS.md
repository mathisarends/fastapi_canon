# AGENTS.md

Guidance for AI coding agents working on this package. The workspace-wide rules
in the root [`AGENTS.md`](../../AGENTS.md) take precedence.

## Project

`fastapi_canon.integrations.websockets` (in the unified `fastapi-canon` distribution) turns decorated channel functions and mounted service endpoints
into a JSON-RPC 2.0 protocol over WebSockets. FastAPI is a required dependency.

| Area | Modules |
| --- | --- |
| Definition | `channel.py`, `protocol.py`, `service.py` |
| Serving | `runtime.py`, `dispatch.py`, `server.py`, `adapters/fastapi.py` |
| Contract | `schema/` (OpenRPC) |
| Client generation | `codegen/`; Python clients embed `codegen/templates/python/runtime/` |

## Commands

Run from the workspace root, using the root environment and lockfile:

```console
uv sync --locked --all-packages --all-groups
uv run --locked python scripts/check.py lint
uv run --locked python scripts/check.py types
uv run --locked python scripts/check.py test
```

Do not run this suite in the same pytest process as other workspace packages.
Install pre-commit only from the workspace root.

## Compatibility

- Preserve wire behavior and generated-client contracts. A change to either is a
  breaking change, even when the Python implementation is small.
- Before 1.0, a breaking change needs the next minor version with patch zero
  (`0.x.y` to `0.(x+1).0`). This includes public signature changes, stricter
  validation and changed defaults. Record migration steps in Canon's `CHANGELOG.md`
  under `### Breaking changes`.
- When an intentional breaking change supersedes a Python API, remove the old
  API instead of keeping aliases or parallel implementations, and update guides,
  tests and generated clients in the same change.
- Build only on documented, public FastAPI and Starlette APIs.

## Conventions

- The public API is re-exported from `../fastapi-canon/fastapi_canon/integrations/websockets/__init__.py` with relative imports.
- No module-level docstrings. Document a class or function only where the name is not enough.
- Generators read the OpenRPC document, never the `RpcProtocol` object, so a new
  target language only needs an emitter beside `codegen/python.py` on top of `codegen/ir.py`.
- Generated code must pass `ruff check` and `ruff format` in the consuming repository.
  Change generated clients through generators/templates, then check for drift.
- Tests live in `tests/` and mirror the package structure.
- Canon owns package metadata; this directory retains its test and tool settings.
