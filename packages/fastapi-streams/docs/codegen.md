# Contracts and client generation

[Documentation](README.md) · [Project](../README.md)

## Export and generate

```sh
uv run canon sse schema \
  --app examples.agent_runs:app --output tests/fixtures/agent_runs.openapi.json

uv run canon sse generate \
  --schema tests/fixtures/agent_runs.openapi.json --language typescript --output frontend/generated

uv run canon sse generate \
  --schema tests/fixtures/agent_runs.openapi.json --language python --output myapp/generated
```

No running server is needed. Export imports the application. The codegen extra
adds Jinja and JSON Schema validation; importing the server package needs neither.

The exporter copies native FastAPI output, promotes compatible 3.1 output to
OpenAPI 3.2.0, updates the declared OAS dialect when necessary, and validates the
official bundled OpenAPI structural schema, Draft 2020-12 Schema Objects and
local references. Incompatible contracts fail explicitly. The application's
cached document and ordinary REST endpoints remain intact.

The generator extracts application data from
`itemSchema.properties.data.contentSchema`. Only stream operations and models
reachable from their parameters, bodies, events and error responses are emitted.
Operations use the first tag as their group, defaulting to `streams`.

Generated method documentation carries each operation's `summary`, `description`,
and `deprecated` marker alongside language-specific SDK behavior. Describe
replay, retention, and server job lifetime explicitly on your endpoint; the
generator does not infer these business guarantees. Python early exits should
use `contextlib.aclosing`; TypeScript `for await` cleanup closes its reader.

TypeScript has no runtime dependencies and uses Fetch-compatible APIs. The
example uses ES modules and is tested on Node 24. Generated Python packages
require Python 3.12+, `httpx>=0.28` and `pydantic>=2.11.2` (`uv add httpx pydantic`).

HTTPX is the default. To generate a Python client using HTTPX2 instead, opt in
when generating and checking it:

```sh
uv run canon sse generate \
  --schema tests/fixtures/agent_runs.openapi.json --language python \
  --python-http-client httpx2 --output myapp/generated

uv run canon sse check \
  --schema tests/fixtures/agent_runs.openapi.json --language python \
  --python-http-client httpx2 --output myapp/generated
```

Install `httpx2>=2.13.1,<3` and `pydantic>=2.11.2` in that consumer. The
generated package needs only the selected HTTP library; installing HTTPX2 does
not change an existing HTTPX client. The Python generator API accepts the same
choice as `generate_python(contract, http_client="httpx2")`. Pass the choice to
every `check` or `generate --check` call; omitting it checks HTTPX output.
`--python-http-client` requires `--language python`.

REST operations remain available in the contract for an existing SDK generator
such as Orval or HeyAPI. Keep REST and streaming SDKs in separate output
directories. Reusing external generated model imports is a future feature.

## Event discriminators

A Python default allows omitting a field when constructing a model. It does not
automatically make the field required in JSON Schema. The generator respects
`required` and warns with `OptionalDiscriminatorWarning` when a stream event
union uses an optional discriminator, including nested unions. An absent tag may
prevent variant selection; TypeScript's static types do not validate incoming JSON.

For **output-only** event models whose serializer always emits default fields,
configure the output schema to describe that behavior:

```python
from pydantic import BaseModel, ConfigDict


class StreamEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_mode_override="serialization",
        json_schema_serialization_defaults_required=True,
    )
```

Inherit event variants from this model, or apply the configuration to an existing
output-only base. It marks all serialized default fields required, not only the
discriminator. Avoid applying it globally to shared request models: input and
output requiredness can differ. Alternatively, declare the tag without a default
and pass it explicitly. Regenerate the schema and clients after changing models.

Warnings are advisory and do not change fields or defaults. Python callers can
filter or promote this category using the standard `warnings` module; the category
is available from `fastapi_canon.integrations.sse.codegen.diagnostics`.

## Check drift in CI

```sh
uv run canon sse check \
  --app examples.agent_runs:app --schema tests/fixtures/agent_runs.openapi.json \
  --language typescript --output examples/agent_runs/generated

uv run canon sse check \
  --app examples.agent_runs:app --schema tests/fixtures/agent_runs.openapi.json \
  --language python --output examples/agent_runs/generated_python
```

`check` imports the app, compares its contract, validates the stored schema and
compares generated files without writing. Omit `--app` for a contract supplied
by another service. `schema --check` and `generate --check` are also supported.
Exit codes: **0** current, **1** drift, **2** invalid input.

Add `--diff` to any check (`schema --check`, `generate --check`, `check`,
`sync --check`) to print a unified diff of each out-of-date file to stdout. The
`Out of date:` lines stay on stderr. New files diff against `/dev/null`, and so
do stale files the manifest owns but generation no longer emits. A changed
application contract prints the stored schema against the app's export. `--diff`
without `--check` is rejected with exit code 2.

### One command for schema and clients

Configure the schema and its clients once in `pyproject.toml` (or another TOML
file passed with `--config`):

```toml
[tool.fastapi-streams]
app = "myapp.main:app"            # optional; omit for an externally supplied schema
schema = "contracts/openapi.json"

[[tool.fastapi-streams.clients]]
language = "typescript"
output = "web/src/generated"

[[tool.fastapi-streams.clients]]
language = "python"
output = "sdk/generated"
python-http-client = "httpx"      # optional, Python only
strict = true                     # optional
allow-diagnostics = "reviewed-python.json"  # optional
diagnostics = "reports/python.json"         # optional
```

```sh
uv run canon sse sync            # export the schema, then regenerate every client
uv run canon sse sync --check    # read-only CI check of schema and all clients
uv run canon sse sync --check --diff
```

Paths and the app import resolve relative to the configuration file. `sync` runs
`schema` (when `app` is set) and then `generate` for each client with the same
options as the individual commands, so diagnostics, manifests and exit codes
behave identically. A check reports every stale target before exiting with **1**.
It compares clients with the stored schema; when that schema is missing, client
checks are skipped. Unknown keys and invalid values fail with exit code **2**.

Generation is deterministic. A manifest tracks owned files and cleans up stale
generated files without deleting neighboring handwritten files. Use a dedicated
output directory for each language. Unsafe paths and generated-name collisions
fail explicitly.

### SDK provenance

CLI generation always writes `.fastapi-streams.json` alongside the SDK. Commit
this file with the generated sources. Its existing `files` list still controls
ownership and cleanup; the additional `generation` object contains the same
provenance as an optional diagnostic report:

| Field | Meaning |
| --- | --- |
| `generator`, `generator_version` | Generator distribution and version |
| `schema_sha256` | SHA-256 of the complete source OpenAPI document |
| `schema_hash_encoding` | `canonical-json-v1`: JSON with recursively sorted keys, compact separators and UTF-8 without ASCII escaping |
| `target` | `python` or `typescript` |
| `python_http_client` | `httpx` / `httpx2` for Python, JSON null for TypeScript |

There are no timestamps or local paths. Formatting and object-key order do not
affect the schema hash; changes to any schema content do, including descriptions
and components unused by the SDK. This identifies the input and configuration;
it is not a signature or an integrity check of the generated sources.

`check` compares the manifest as well as SDK sources. A different generator
version or source hash is drift even when the emitted code is identical.
Regenerate once with the chosen language and existing HTTP backend to upgrade a
legacy files-only manifest, then commit the manifest diff. Checks remain
read-only; generation retains neighboring handwritten files and only removes
previously owned stale files. No diagnostic report is required for provenance.
The low-level `write_files` API accepts optional `generation` metadata; without
it, its files-only behavior remains available for callers writing raw file maps.

## Runtime validation and unknown variants

Python validates path, query and header parameters and JSON request bodies at the
method call, before serialization or HTTP I/O. Invalid arguments raise Pydantic
`ValidationError` immediately, even if the returned iterator is never consumed.
HTTP starts when iteration begins. Optional parameters supplied as `None` are
omitted, preserving existing omission semantics. Pydantic's normal coercion
applies; this is not Pydantic strict mode. Valid scalar encoding, integer values
of number parameters (`0` stays `0`), query array explosion and body aliases are
preserved. Adapters are cached in each generated API class.

Python validates generated event models and request bodies with Pydantic. Supported
scalar constraints are `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`,
`exclusiveMinimum`, `exclusiveMaximum`, and `multipleOf`. They survive nested
models, arrays, nullable types and referenced scalar aliases. Patterns use
Pydantic's Rust regex engine; unsupported expressions fail generation. Numeric
multiples follow Pydantic's floating-point tolerance. This is not a complete JSON
Schema validator. Formats, collection/object limits and other unsupported rules
emit `UnsupportedConstraintWarning`; promote it to an error in CI when required.
Constraints alongside enum/const or composition fail explicitly; constraints on
scalar branches are supported. TypeScript provides static shapes and does not
validate incoming JSON or enforce scalar constraints at runtime.

Python event unions are closed by default: unknown variants raise
`StreamParseError`. For an upstream that emits more variants than your declared
subset, explicitly choose a per-request policy:

```python
from generated import RequestOptions

options = RequestOptions(unknown_events="ignore")
async for event in client.runs.run_events(run_id="run_abc", options=options):
    ...
```

Ignoring requires a top-level discriminated union with known string tags. Only
valid JSON objects with an unknown string discriminator are skipped. Invalid
JSON, missing/non-string discriminators and malformed known variants still fail;
unknown nested variants inside a known event fail too. The option adds no retry,
reconnect or replay. Regenerate existing Python SDKs to obtain the option.

TypeScript does not validate event shapes, so it forwards every JSON-decoded
value by default (`unknownEvents: "forward"`). An exhaustive `switch` over the
event tag then reaches its `never` branch for a variant the client does not
know. Generated operations embed their union's discriminator and known tags;
`unknownEvents: "ignore"` skips JSON objects whose discriminator is an
undeclared string tag:

```typescript
for await (const event of client.agentRuns.streamRunEvents(
  { runId: "run_abc" }, { unknownEvents: "ignore" },
)) {
  handle(event);
}
```

Objects without a string tag, arrays and scalars are still forwarded, and invalid
JSON still throws `StreamParseError`; known variants are not validated. An
operation without a top-level discriminated union rejects `"ignore"` with a
`TypeError` before the request is sent. Regenerate TypeScript SDKs to obtain the
option.

## Validation support matrix

| SDK target / boundary | Runtime shape validation | Supported scalar constraints | Timing |
| --- | --- | --- | --- |
| Python path/query/header | Yes | Yes, including referenced scalars and query array items | Method call, before serialization |
| Python JSON request body | Yes | Yes, including nested values | Method call |
| Python SSE event data | Yes | Yes, including nested values | Each event during iteration |
| Python declared HTTP error bodies | Explicit consumer TypeAdapter | When explicitly validated | Consumer-controlled |
| TypeScript parameters/body/events | Static types; events are JSON-decoded | No runtime enforcement | Generation diagnoses limitations |

Unsupported formats and collection/object constraints remain diagnostics rather
than implicit runtime validators. Optional `None` query/header parameters mean
omission, not JSON null. Server-side validation remains necessary for every client.

## Source diagnostics and CI policy

`generate`, `generate --check` and `check` accept these options:

```sh
uv run canon sse check --schema openapi.json --language python \
  --output generated --diagnostics reports/python.json --strict \
  --allow-diagnostics reviewed-python.json
```

Omit `--allow-diagnostics` when no limitations are accepted. The JSON report
(`version: 1`, `diagnostics: [...]`) includes each diagnostic's `id`, `code`,
`pointer`, `keyword`, `value`, `target`, `operation_id`, `method`, `path`, `message`
and `accepted` status. Pointers are RFC 6901 paths into the original document,
including inline schemas and referenced component definitions. Shared definitions
identify each affected operation; unused components do not generate warnings.
SSE framing metadata is outside generated event-model validation.

The version 1 report also carries additive `generation` and `validation` objects.
Consumers should tolerate additional object keys. `generation` records the
generator name/version, target, selected Python HTTP library (null for TypeScript)
and `schema_sha256`. The hash covers the complete parsed OpenAPI document,
including unused definitions. Its `canonical-json-v1` encoding is Python
`json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
encoded as UTF-8. Whitespace and object-key order in the input file do not affect
the hash; array order and values do. It is a content identifier, not a signature
or proof of schema enforcement.

`validation` distinguishes parameter/body validation at `method_call`, event
decoding at `iteration` and HTTP-error handling at `consumer_controlled`.
TypeScript parameter/body schema checks are `not_performed`. Each boundary has
a `schema_validation` boolean, with `json_decoding: true` for events. Python's
`runtime_scope` is `generated_types_and_supported_constraints`; TypeScript's is
`json_decoding_only`. Both set `full_json_schema: false`. These target capabilities
describe supported boundaries, even if an operation has no parameters or body;
source-specific limitations remain in `diagnostics` with their accepted status.
The report is generated only when requested and adds no SDK runtime checks.

`unsupported-constraint` identifies an unenforced rule for the selected target;
TypeScript also diagnoses Python-supported scalar constraints because TypeScript
has no runtime schema validator. Python's `optional-discriminator` diagnoses event
selection risks without changing field requiredness. Human-readable warnings
contain the same location, keyword/value, operation and limitation as the report.

Without `--strict`, unaccepted diagnostics remain warnings. With it, any
unaccepted diagnostic fails with exit code 2 before generated files change; drift
still uses exit code 1. Reports are written even on strict-policy failure. Keep
the report outside generated output and separate from schema/allowance inputs.

Review the report and explicitly record an allowance, for example:

```json
{
  "allow": [
    {
      "id": "unsupported-constraint:<copy the exact ID from the report>",
      "reason": "Reviewed upstream UUID validation; client remains a string."
    }
  ]
}
```

IDs include code, source pointer, keyword/value, target and operation context;
message wording and schema filename are excluded. Changed constraints or contexts
need a new review. Empty reasons, duplicate IDs, malformed files and stale
allowances fail explicitly. Keep separate policies for each contract/target.
Repository drift checks use strict mode with reviewed TypeScript allowances.

Python generator API users can collect the same objects with
`build_ir(document, target="python", diagnostics=items)` where `items` is an empty
list. Omitting `diagnostics` emits warnings; the default target is Python. Select
the matching target explicitly before calling the corresponding generator.

## Existing native streams and mixed REST generation

See the [native transport and mixed-codegen recipe](native-transports.md) when
an existing generator, native server or byte-preserving proxy owns the stream.
