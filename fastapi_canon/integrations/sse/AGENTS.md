# Repository guidance

`fastapi_canon.integrations.sse` turns typed FastAPI SSE generators into an OpenAPI 3.2 contract
and generated TypeScript/Python clients. Follow the repository rules in the root
[`AGENTS.md`](../../../AGENTS.md); this file adds integration-specific conventions.

## Layout

- `fastapi_canon/integrations/sse/router.py`: `StreamRouter` and endpoint validation.
- `fastapi_canon/integrations/sse/codegen/`: export (`openapi.py`), shared HTTP IR (`ir.py`),
  emitters (`python.py`, `typescript.py`), CLI (`cli.py`) and diagnostics.
- `fastapi_canon/integrations/sse/codegen/templates/`: generated runtimes. `python/runtime.py`
  and `sse.ts` are copied verbatim into every generated SDK.
- `examples/sse/agent_runs/` and `tests/fixtures/sse/` contain committed generated SDKs.

## Commands

Run from the repository root after `uv sync --locked --all-groups`
and `pnpm install --frozen-lockfile --ignore-scripts`:

```console
uv run --locked python scripts/check.py test
uv run --locked python scripts/sse/codegen.py
uv run --locked python scripts/check.py clients
pnpm run test:sse
pnpm run typecheck:sse
uv run --locked python scripts/check.py lint
```

All tests run from the repository root with qualified `tests.*` fixture imports.
For a focused run, use `uv run --frozen pytest tests/unit/integrations/sse`.

## Python module layout

In new or edited Python source modules, put the public API (public classes and
functions) first, immediately after imports and module constants. Define private
implementation helpers below the public API. Keep the executable `__main__`
entrypoint at the end of the module.

## Generated clients

Generated client files are owned by the generators; update their templates rather
than editing generated output by hand. After a template change, run
`scripts/sse/codegen.py` to regenerate every committed SDK and its manifest, then run
the Python and TypeScript tests. Runtime changes that alter generated code need a
changelog entry telling consumers to regenerate.
