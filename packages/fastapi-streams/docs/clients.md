# TypeScript and Python clients

[Documentation](README.md) · [Project](../README.md)

## Named values and typed iteration

Discriminated unions produce an enum named after the union and discriminator:
`RunEvent` + `type` becomes `RunEventType`. Named string enums and multi-value
literal model fields also get named values. Single constant fields, such as
problem titles and URIs, remain inline literals. Python emits `StrEnum` for enums;
TypeScript emits an `as const`
object plus a union type. Both retain exhaustive discriminator narrowing.
Parameters use camelCase in TypeScript and snake_case in Python; wire aliases
are preserved.

Optional scalar query and header parameters treat TypeScript `null`/`undefined`
and Python `None` as omission, so FastAPI applies its default. Required query and
header parameters reject these values with `TypeError` before sending a request:
there is no JSON-null representation in the supported HTTP parameter encoding.
The string `"null"`, `false`, zero, empty strings and query array elements retain
their ordinary serialization. Nullable JSON body fields still use JSON null.

```typescript
import { createClient, RunEventType } from "./generated/index.js";

const client = createClient({
  baseUrl: "http://localhost:8000",
  headers: { Authorization: "Bearer demo-token" },
});
const controller = new AbortController();
for await (const event of client.agentRuns.streamRunEvents(
  { runId: "demo" }, { signal: controller.signal },
)) {
  switch (event.type) {
    case RunEventType.RUN_STARTED: console.log(event.run_id); break;
    case RunEventType.TOKEN_DELTA: console.log(event.text); break;
    case RunEventType.RUN_COMPLETED: console.log(event.run_id); break;
    case RunEventType.RUN_FAILED: console.error(event.message); break;
    default: {
      const exhaustive: never = event;
      throw new Error(`Unexpected event: ${exhaustive}`);
    }
  }
}
```

```python
from myapp.generated import Client, RunEventType

async with Client(
    "http://localhost:8000", headers={"Authorization": "Bearer demo-token"}
) as client:
    async for event in client.agent_runs.stream_run_events(run_id="demo"):
        match event.type:
            case RunEventType.TOKEN_DELTA:
                print(event.text, end="", flush=True)
            case RunEventType.RUN_COMPLETED:
                print(event.run_id)
```

For POST, TypeScript accepts `streamPrompt({ runId, body: { prompt: "Hello" } })`.
Python accepts `stream_prompt(run_id="demo", body=RunRequest(prompt="Hello"))`;
import `RunRequest` from the generated `models` module.

## Python HTTP library

Generated Python clients use HTTPX by default. Generate with
`--python-http-client httpx2` to opt in to HTTPX2 and install
`httpx2>=2.13.1,<3` alongside Pydantic in the consuming project. Client methods,
event models and SDK errors stay the same. See the [generator options](codegen.md).

When injecting a client, use the library selected at generation time:

```python
import httpx2
from myapp.generated import Client

async with httpx2.AsyncClient(timeout=30) as http:
    async with Client("http://localhost:8000", http_client=http) as client:
        async for event in client.agent_runs.stream_run_events(run_id="demo"):
            print(event)
```

Use `httpx.AsyncClient` for the default generated client. Each generated variant
uses its library's native client, response and exception types; Pyright rejects
injection from the other library. HTTPX2 clients follow HTTPX2's native transport
and TLS defaults; see its [migration guide](https://pydantic.dev/docs/httpx2/get-started/migration/).

## Cancellation and errors

TypeScript accepts a custom `fetch`, base URL, client/request headers,
credentials and an `AbortSignal`. Breaking `for await` cancels the reader and
aborts the request. Parsing handles arbitrary byte boundaries, fragmented UTF-8,
LF/CRLF/CR, multiline data, comments, heartbeats and final EOF events.

Python accepts an injected async HTTP client and per-call `RequestOptions`.
Injected clients stay caller-owned. Cancelling a consuming task closes its
response.

Requests inherit the injected client's timeout unless explicitly overridden
with `RequestOptions(timeout=seconds)`. `RequestOptions(timeout=None)` disables
timeouts for that request. The SDK-owned client (no `http_client` passed) uses
`httpx.Timeout(None, connect=10.0)`, exported as `runtime.DEFAULT_TIMEOUT`: an
unreachable host fails after 10 seconds, while read, write and pool waits stay
unbounded so quiet streams are not cut off. Inject a client or pass
`RequestOptions(timeout=...)` for different limits.

Use `aclosing` for deterministic cleanup when exiting early:

```python
from contextlib import aclosing

async with aclosing(client.agent_runs.stream_run_events(run_id="demo")) as events:
    async for event in events:
        print(event)
        break
```

Both SDKs expose `HttpError` (status, response and available JSON/text details),
`StreamParseError` (invalid event data) and `TransportError` (network/protocol
failure). HTTP diagnostics are bounded to 64 KiB. Python validates events with
Pydantic; TypeScript provides static types and JSON decoding, with no runtime
schema validator. Cancellation propagates normally; application error events
remain ordinary members of the user-defined union.

## HTTP error contracts

Keep schema validation at transport boundaries. Python prepares and validates
requests at the method call and validates received events during iteration.
Pass those parsed event models onward instead of serializing them to dictionaries
and validating the same schema again. Server request validation remains necessary
regardless of SDK checks; authentication, authorization and domain invariants have
separate responsibilities. Canon composition does not need an additional event
validator around Streams. TypeScript keeps its documented static-only behavior;
applications needing runtime event checks can validate once before using a value.

Python request parameters and JSON bodies are validated when calling the method;
catch Pydantic `ValidationError` around that call. Network and event errors occur
while iterating. See the [validation matrix and CI policy](codegen.md#validation-support-matrix).

Generated Python `StreamParseError` provides these public fields:

| Field | Meaning |
| --- | --- |
| `operation_id` | Source OpenAPI operation ID |
| `category` | `invalid_json` for malformed JSON; `invalid_event_schema` for invalid event shapes/constraints |
| `validation_errors` | Pydantic error dictionaries with `type`, `loc`, `msg`; input values, context and URLs are excluded |
| `data` | Original SSE data payload, retained for explicit inspection |

For errors raised by generated operations, category and operation ID are always
set. The existing `StreamParseError(data)` constructor remains valid; manually
constructed errors can have `None` provenance/category and empty details.
The original Pydantic `ValidationError` remains chained as `__cause__`.

```python
try:
    async for event in client.runs.run_events(run_id="run_abc"):
        handle(event)
except StreamParseError as error:
    record_failure(error.operation_id, error.category, error.validation_errors)
```

The SDK does not log event payloads or exceptions automatically. Raw `.data` and
the full chained exception may contain sensitive values; log only reviewed fields.
Even structured messages and field names should follow the consumer's logging
policy. Malformed known events still fail with `unknown_events="ignore"`; only
valid JSON objects with unknown string discriminator tags can be skipped.

The SDK works with ordinary OpenAPI response declarations, including
`application/json`, `application/problem+json`, and other declared response media
types. No error registry or particular problem envelope is required. Declare
HTTP failures with FastAPI's `responses=`; put authentication, authorization,
and resource checks in dependencies so they can fail before the response starts.
Even generator statements before the first `yield` execute after response start.

Each operation with declared error schemas exports an `OperationErrorBody`
union: for example `StreamRunEventsErrorBody`. The earlier `OperationError`
name remains an alias for compatibility; it describes response bodies, not
the exceptions thrown by the SDK. Python imports these aliases from `models`;
TypeScript exports them from the SDK entrypoint.

Catch HTTP failures around **iteration**, since opening the request is lazy.
`HttpError.details` deliberately remains unvalidated: proxies and servers can
return unexpected JSON, plain text, HTML, incomplete bodies, or no body at all.
An operation's body union does not associate each variant with a status and does
not prove which operation produced an exception. Check the actual HTTP status
and validate the body before using application fields.

TypeScript provides `isHttpError(error, isDetails)`. Supply a type guard that
checks every field promised by its return type, or adapt an existing runtime
validator. The helper verifies the SDK exception class and invokes that guard;
it does not generate a schema validator or validate the HTTP status. A generic
type argument alone cannot replace the required guard. Failed guards leave the
original `HttpError` available for generic handling.

Python can validate `error.details` with `TypeAdapter(OperationErrorBody)` and
handle `ValidationError` as an unexpected response. Both approaches retain
the original status, response, and diagnostics. Schema unions with permissive
variants may accept a broad range of bodies; inspect the parsed variant before
using its fields. Existing limits on bounded HTTP diagnostics still apply.

### Optional consumer decoders

Build a decoder once per operation/status around the existing guard hook when
several callers need the same declared error body. For example, this adapter
accepts a consumer-provided validator with no additional SDK dependency:

```typescript
import { isHttpError, type StreamRunEventsErrorBody } from "./generated/index.js";

function decodeValidationError(
  error: unknown,
  isBody: (value: unknown) => value is StreamRunEventsErrorBody,
): StreamRunEventsErrorBody | undefined {
  if (isHttpError(error, isBody) && error.status === 422) return error.details;
  return undefined;
}
```

Supply a handwritten guard or an existing validator that checks every field of
the generated alias. A type assertion alone does not validate a response. Guards
must validate the original value: if a decoder coerces or transforms data, use
its successfully parsed output instead of narrowing the original `error.details`.
Use this helper only for the operation whose errors the alias describes.

If decoding returns `undefined`, continue handling the original `HttpError`
generically or rethrow it. Its status, response and JSON/text details remain
available, including unexpected proxy responses. Validators run only for SDK
HTTP errors; `isHttpError` leaves network/stream failures alone. Status checks
remain explicit because a matching body does not establish an HTTP status.
The [TypeScript example](../examples/agent_runs/errors.ts) includes a complete
guard and reusable decoder with this fallback behavior.

In Python, construct and reuse an adapter rather than rebuilding it for each
exception:

```python
from pydantic import TypeAdapter, ValidationError
from myapp.generated import HttpError
from myapp.generated.models import StreamRunEventsErrorBody

error_body = TypeAdapter(StreamRunEventsErrorBody)


def decode_validation_error(error: HttpError) -> StreamRunEventsErrorBody | None:
    if error.status != 422:
        return None
    try:
        return error_body.validate_python(error.details)
    except ValidationError:
        return None
```

Use the returned parsed body on success and retain the original exception for
fallback handling. This alias describes an object response, so `None` can
represent failure here; for a nullable body alias, use a separate success flag
or sentinel. Neither recipe changes SDK defaults or automatically validates all
HTTP failures. Diagnostic reports correctly retain `consumer_controlled` for
HTTP-error body validation.

The checked consumer examples show HTTP validation errors, fallback handling,
stream parsing/transport errors, and ordinary application error events:

- [TypeScript error handling](../examples/agent_runs/errors.ts), compiled with the
  generated SDK under the repository's strict TypeScript configuration.
- [Python error handling](../examples/agent_runs/errors.py), checked by Pyright
  with the generated models.

Caller cancellation continues to propagate. Neither SDK converts application
error events into exceptions or automatically retries a stream.

## Python model fields

Python uses `None` for omitted optional fields; `model_fields_set` distinguishes
absence from explicit null. Explicit null is rejected for fields that may only
be omitted. Requests exclude unset fields and serialize using wire aliases.

Python models preserve explicit `additionalProperties: false` with
`extra="forbid"`; unknown request fields raise validation errors and forbidden
event fields raise `StreamParseError`. Explicit unrestricted/typed extra fields
are retained and typed extras are validated. Schemas omitting the keyword keep
their existing default behavior. Python rejects closed-object `allOf` combinations
that introduce fields forbidden by another member, or incompatible typed extras.
