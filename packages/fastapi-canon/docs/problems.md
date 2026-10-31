# Shared problem contracts

Canon 0.10.0 introduces the common rendering and schema foundation for the
canonicalization design. The release is prepared source, not a publication.
HTTP and WebRTC admission use the shared schema compiler. New RPC envelopes,
terminal SSE events, and the common generated SDK facade are subsequent work.

## Identity and public data

[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) defines `type` as the
primary problem identity. Canon uses stable absolute type URIs and adds a stable
`code` extension for convenient application handling. `title` describes the
problem type; `detail` describes an occurrence. Neither is a control-flow key.

The Canon emission profile requires `type`, `title`, and `code`; these are Canon
requirements, not a claim that the RFC requires all three. HTTP occurrences also
carry their actual response status. Optional standard members are omitted when
absent. Explicitly null extension values remain public JSON data.

Define ordinary domain exceptions and map them at the API boundary:

```python
from fastapi_canon import Error, ErrorRegistry


class ProjectMissing(Exception):
    pass


project_missing = Error(
    ProjectMissing,
    status=404,
    code="project_missing",
    title="Project not found",
    detail="Choose another project.",
)
errors = ErrorRegistry(
    errors=[project_missing],
    type_base="https://api.example.com/problems",
)
```

## Render an occurrence

```python
problem = errors.problem_for(
    ProjectMissing("internal information"),
    instance="urn:uuid:983cf916-6e8e-4d8f-a836-e9708d160890",
)
assert problem is not None
payload = problem.as_dict()
```

The payload has the registered `type`, `title`, `code`, reviewed `detail`, and
provided `instance`. The exception text is not exposed automatically.
Use `as_dict()` for wire output; Pydantic's generic `model_dump()` does not apply
Canon's omission rules for optional standard members.

`problem_for()` resolves the most specific registered exception through Python's
MRO. It returns `None` for an unmapped exception and raises
`ErrorConfigurationError` when a mapped type URI is unresolved or a renderer
produces invalid data. The embedding adapter owns safe handling of callback
failures; HTTP handlers retain their logged internal-problem fallback.

HTTP header callbacks are not evaluated by `problem_for()`. Its default
`include_status=False` omits HTTP status, suitable for a documented embedding
without a corresponding HTTP error response. Pass `include_status=True` when
rendering an HTTP occurrence, and send that same status in the actual response.
The supplied `instance` is occurrence data, not an automatic request URL or trace
identifier.

## Reuse the installed application catalog

Feature-local registries may defer type URI resolution to composition. Retrieve
the installed catalog instead of rebuilding its application policy:

```python
catalog = ErrorRegistry.from_app(app)
problem = catalog.problem_for(ProjectMissing())
```

This returns the same immutable, resolved registry installed by composition or
`ErrorRegistry.install()`. An application without an installed registry raises
`ErrorConfigurationError`. This API does not install normalizers or change their
defaults.

## Describe an embedding

```python
embedded_schema = errors.schema_for(project_missing)
http_schema = errors.schema_for(project_missing, include_status=True)
```

Each call returns a detached JSON Schema object with `$id` equal to the resolved
problem type URI. Nested extension references use local `#/$defs/...` paths and
require no FastAPI OpenAPI document. Schemas and documented examples omit HTTP
status by default. Registry membership and URI resolution are checked exactly as
for rendering.

HTTP compilation adapts this same schema to OpenAPI component references and
includes the mapped status. CanonWebRtcRouter uses the self-contained HTTP form
for pre-accept admission problems. This preserves its signaling protocol.

## OpenAPI and migration from 0.9.0

Same-status Canon problem unions now discriminate on `type`, with URI keys in
the discriminator mapping. This is resolved after composition supplies the
application type base. `code` remains present in runtime payloads and schemas.
Manually authored response schemas remain application-owned.

Regenerate clients after upgrading. Schema consumers that explicitly inspect
the old `code` discriminator must switch to `type`. Business-specific problems
need distinct type URIs. A domain problem with `type="about:blank"` cannot share
a union with a generic problem that has that same identity; such ambiguous
unions fail compilation without caching a partial OpenAPI document.

`Problem.status` is now optional for embeddings. Code reading it directly must
handle `None`. When supplied it must be an actual integer: booleans, numeric
strings, and floats are rejected. HTTP responses still include their mapped
integer status. Missing `detail` and `instance` are represented by omission, not
JSON null; typed extension fields can still explicitly contain null.

The SSE terminal-problem profile and JSON-RPC problem-envelope adapter described
in the root decision record are design targets. `problem_for()` and
`schema_for()` supply their common foundation without introducing those wire
changes in this step.
