# Contributing

Use Python 3.12–3.14, uv 0.9.2 or newer, Node.js 24 and pnpm 12.14.0.
Install pnpm following its [installation guide](https://pnpm.io/installation);
`packageManager` in the root `package.json` pins the version used locally and in CI.
Run commands from the repository root; the repository has one Python environment
and `uv.lock`, plus one `pnpm-lock.yaml` for all TypeScript tooling.

```console
git clone https://github.com/mathisarends/fastapi_canon.git
cd fastapi_canon
uv sync --frozen --all-groups
pnpm install --frozen-lockfile --ignore-scripts
uv run --locked python scripts/check.py all
uv run --locked python scripts/check_distributions.py
uv run --locked python scripts/check_distributions.py --python 3.12 --dependencies minimum
uv run --locked python scripts/check_streams_artifacts.py
uv run --locked python scripts/check_webrtc_artifacts.py
```

`check.py` accepts `architecture`, `lint`, `types`, `test`, `clients` and `browser`
individually. The [Smelt policy](architecture-guardrails.md) runs first in `all`
and also runs through the root pre-commit hook; warnings fail the gate.
All library tests run together from the root using qualified test/example
imports. For a focused suite, select its path:

```console
uv run --frozen pytest tests/unit/error tests/integration/error tests/contract/error
uv run --frozen pytest tests/unit/integrations/websockets
```

`uv run --frozen pytest` runs all unit, integration and contract tests. The
reference service also runs its own application tests through `check.py test`.
Feature-specific type/lint settings live under `tooling/`; they do not define
additional packages or environments. See [repository architecture](architecture.md)
for the folder map and dependency boundaries.

The `browser` gate is separate from `all`. Install Chromium once at the root with
`pnpm exec playwright install chromium`, then run
`uv run --frozen python scripts/check.py browser`. It exercises real Chromium
peers against aiortc. RPC tests use the root's locked TypeScript and Prettier
dependencies with `pnpm exec`; they do not download tools during tests.
Use `pnpm typecheck` and `pnpm test:coverage` for all TypeScript gates, or
`pnpm run typecheck:sse` for a focused check. Regenerate committed clients through
their generators before checking drift; never edit generated clients directly.

`check_distributions.py` builds wheels/sdists and tests eleven feature/tool
combinations of the single Canon wheel in fresh environments without source-tree
overrides. Current and minimum dependency profiles are separate checks.
The current profile also installs the development-only reference service and
tests it without transport extras, then installs its optional telemetry
dependencies and checks SQL/HTTP trace contracts.
The dedicated Streams and WebRTC artifact scripts verify installed CLIs and the
clients they generate. Build outputs and dependency inventories belong in ignored
`dist/`, not in versioned development reports.

Only root `.github/workflows/` and the root pre-commit configuration are active.
Optionally run `uv run --locked pre-commit install` from the root. Release metadata
and publication commands are documented in [releases](releases.md).

Run the composed HTTP/SSE/RPC example with:

```console
uv run --locked uvicorn examples.stack_service:app --reload
```

`POST /counter` changes shared state, `GET /counter/events` streams it and `/rpc`
accepts `counter.increment`. The [reference service](../examples/reference-service)
demonstrates feature slices, authentication, persistence, transactions and HTTP
error contracts. The [remote video example](../examples/webrtc/remote_video)
demonstrates per-session engine bindings and lease cleanup.
