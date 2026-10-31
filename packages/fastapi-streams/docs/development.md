# Development

[Documentation](README.md) · [Project](../README.md)

Use the [workspace contributor commands](../../../docs/contributing.md) from the
repository root. They use one lockfile and run each package in its own process.
Only the root CI and pre-commit configurations are active.

For focused Streams work:

```console
uv run --locked --project ../.. --directory packages/fastapi-streams python -m pytest -q
uv run --locked --project ../.. --directory packages/fastapi-streams python scripts/codegen.py --check
npm run typecheck --prefix packages/fastapi-streams
npm run test:coverage --prefix packages/fastapi-streams
```

Drop `--check` to regenerate committed examples and fixtures through their
generator. Install npm dependencies before pytest to enable TypeScript acceptance
tests. Coverage includes real FastAPI-to-Node/Python requests, strict narrowing,
recursive models, authentication, disconnect cleanup, cancellation and drift.

Dependency and installed CLI checks use the root
`scripts/check_distributions.py` and `scripts/check_streams_artifacts.py`.
See [compatibility](../../../docs/compatibility.md) for their scope.

## Coverage and package checks

Python reports include branch coverage for the library and the executed generated
example transport, with an 85% combined floor. Identical runtime copies in the
fixtures are checked for drift rather than counted twice. TypeScript's generated
example transport has floors of 95% lines, 90% branches and 90% functions.
The root client gate enforces those TypeScript floors. Run pytest with `--cov`
when checking the separate Python coverage configuration.

The schema matrix validates accepted/rejected payloads and runs strict TypeScript
and Python type checks on newly generated packages, including both HTTP backends.
Consumer type tests also assert
that missing arguments, wrong parameter types and invalid event fields are rejected.
Contract boundary tests cover unsupported serialization, references and collisions.

Package checks install the freshly built wheel in three new environments: core
only, codegen with HTTPX only, and codegen with HTTPX2 only. They bypass cached local
wheels so repeated builds of the same release version test the current artifact.
In a directory
outside the checkout they invoke the installed `fastapi-streams` command, exercise
`schema`, `generate`, `check` and drift exit codes, compile the generated TypeScript
and consume an event through the generated Python SDK. Metadata and `__version__`
must match the release version in `pyproject.toml`.

## Generated SDK performance

From the workspace root, use the locked environment and installed Node tooling:

```console
uv run --locked python packages/fastapi-streams/scripts/benchmark_runtime.py --memory --output dist/streams-baseline.json
```

The harness generates both SDKs from the same small discriminated-union contract.
It measures request preparation separately from decoding, framing and complete
HTTP/SSE consumption with an in-memory HTTPX transport or Fetch response. Cases
cover small token events, nested batches, seven-byte fragments and Python's
unknown-event ignore policy. Python validates generated models; TypeScript only
decodes JSON. Their decoding results do not represent equivalent guarantees.

Reports record source location, generator/runtime versions, platform, payload
sizes, individual samples, median CPU/wall time per request or input event, and
throughput. Ignore-policy throughput counts skipped input events too. `--memory`
adds a separate Python traced peak-memory sample; it does not instrument timings
or count total allocations. There are three warmup iterations per case. These
are warmed batch measurements, not individual-event latency percentiles, cold
startup measurements, real-network results or Hermes deployment evidence.

Use `--runtime python` or `--runtime typescript` for focused runs. Adjust
`--events`, `--iterations` and `--rounds` for longer samples. For a comparison,
run the same harness with `--source-dir` pointing to the other revision's Streams
package directory, keeping the interpreter, dependencies and arguments fixed.
Repeat runs to assess noise; ordinary CI does not enforce timing thresholds.

## Supported scope

GET/POST JSON SSE data; scalar path and header parameters; scalar/array query parameters;
JSON bodies; local component references; nested objects, maps, arrays, string
enums and discriminated unions. Query form arrays support both `explode` values.
Python `allOf` supports compatible object intersections. TypeScript does not
express validation constraints such as numeric ranges as refinement types.
Schema references may carry metadata (for example `description`, `title` and
`default`), but semantic siblings such as `enum`, `const`, `required`,
`additionalProperties` or `type` are rejected. Define a standalone schema with
the full restrictions; reference-chain constraints are checked before generation.
Required object keys absent from `properties` inherit the `additionalProperties`
value type (unrestricted when omitted or true). They remain required in both
SDKs. A required undeclared key combined with `additionalProperties: false` is
rejected as an unsatisfiable contract before any client files are written.

Header arrays (Fetch combines repeated headers), external references, cookies,
deepObject/matrix/label styles, multipart bodies,
tuple schemas and object/array enum values are rejected. No REST SDK replacement,
WebSockets, JSON-RPC, persistence, automatic reconnect or replay.

Both shipped generators share a transport-independent HTTP IR.
