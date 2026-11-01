# Contributing

Use Python 3.12–3.14, uv 0.9.2 or newer and Node.js 24. Run commands from the
repository root; the workspace has one environment and one `uv.lock`.

```console
git clone https://github.com/mathisarends/fastapi_canon.git
cd fastapi_canon
uv sync --locked --all-packages --all-groups
npm ci --ignore-scripts --prefix packages/fastapi-streams
npm ci --ignore-scripts --prefix packages/fastapi-webrtc
uv run --locked python scripts/check.py all
uv run --locked python scripts/check_distributions.py
uv run --locked python scripts/check_distributions.py --python 3.12 --dependencies minimum
uv run --locked python scripts/check_streams_artifacts.py
uv run --locked python scripts/check_webrtc_artifacts.py
```

`check.py` accepts `architecture`, `lint`, `types`, `test`, `clients` and `browser`
individually. The [Smelt policy](architecture-guardrails.md) runs first in `all`
and also runs through the root pre-commit hook; warnings fail the gate.
Each feature suite keeps its lint/type settings and runs tests in a separate process:
their top-level `tests` and `examples` namespaces cannot share one pytest run.
Use `uv run --locked --directory packages/fastapi-canon python -m pytest -q` for a focused
suite. For the retained feature directories, run
`uv run --locked --project ../.. python -m pytest -q` from that directory.
`uv run --locked pytest -q` runs only the root integration/release suite.

The `browser` gate is separate from `all`. Install Chromium once from
`packages/fastapi-webrtc` with `npx playwright install chromium`, then run
`uv run --locked python scripts/check.py browser` at the root. It exercises real
Chromium peers against aiortc. RPC TypeScript tests use npx; Streams and WebRTC
have npm lockfiles. Regenerate clients through their package generators before
checking drift; never edit generated clients directly.

`check_distributions.py` builds wheels/sdists and tests eleven feature/tool
combinations of the single Canon wheel in fresh environments without workspace
source overrides. Current and minimum dependency profiles are separate checks.
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
error contracts. The [remote video example](../packages/fastapi-webrtc/examples/remote_video)
demonstrates per-session engine bindings and lease cleanup.
