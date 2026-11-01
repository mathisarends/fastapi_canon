# Architecture and test guardrails

The root `smelt.yml` is the executable policy for the current Canon workspace.
`smelt-cli==0.2.0` is pinned in the root development group and `uv.lock`; the
unrelated `smelt` distribution is not this tool. `.smelt.schema.json` supplies
editor completion for the pinned version.

## Run the policy

```console
uv sync --locked --all-packages --all-groups
uv run --locked smelt context fastapi_canon.error.registry
uv run --locked smelt check --config smelt.yml --changed --fail-on warning
uv run --locked python scripts/check.py architecture
uv run --locked pre-commit run --all-files
```

The `architecture` gate is included in `scripts/check.py all`, and therefore in
the existing CI matrix. The root pre-commit hook runs the same full check on every
commit, including changes to policy or dependency metadata. Both errors and
warnings fail the gate. There is no debt baseline. Strict coverage also applies
during changed-file checks.

## Dependencies and source scope

The policy covers Canon's complete Python source and the reference application.
Every discovered production module needs an architecture role or a reasoned
exclusion. Runtime templates and generated/demo artifacts are excluded because
the existing generator-drift, installed-artifact and consumer suites check them.
The retained transport directories remain separate pytest invocations, as
described in [contributing](contributing.md).

| Responsibility | Policy |
| --- | --- |
| Error values, rendering and metadata | Standard library and Pydantic; no FastAPI, Starlette or Dishka |
| Successful response contracts | Depend on values, without framework dependencies |
| HTTP/OpenAPI adapters | Depend on values and responses |
| Routing and composition | Build on the HTTP adapters; composition owns Dishka and optional telemetry wiring |
| SSE, WebSockets and WebRTC | Separate transport roles; no dependency on sibling transports |
| Transport generators and common SDK planning | Explicit generation role, allowed to consume protocol contracts |
| Static error-contract analysis | Standard library only |
| Testing and reference application | Explicit roles and dependency allowlists |

Checks follow transitive imports and include `TYPE_CHECKING` imports. Third-party
dependencies default to denied, with explicit allowlists for each role. The
read-only `ErrorCatalog` protocol keeps HTTP/OpenAPI consumers independent of the
concrete registry that installs them. Shared configuration exception types live
with the error value types rather than depending back on composition.

## Source conventions

Use descriptive module names, absolute imports in implementation modules and
relative imports in package facades, following the
[import guide](../guides/imports-and-reexports.md). `_version.py` is the explicit
exception to the private-module naming rule because release tooling reads that
file. Internal analysis and socket implementation modules have descriptive names.

Public definitions precede private helpers while preserving definition-time
dependencies. Instance assignments remain separate. Internal helpers are private,
and stateless static helpers belong at module scope. Existing package exports,
redundant `StrEnum.value` accesses and functions exposed only for tests are checked.
Same-subject type dispatch with three or more branches is reviewed for `match`.
Future-annotation checks use the declared Python 3.12 minimum. The established
error-package convention also forbids module docstrings; class and function
documentation remains supported. Ruff's `UP` rules replace the former AST-based
annotation-style tests.

## Tests

Canon's focused tests mirror its source directly under the workspace member's
`tests/` root. For example, `fastapi_canon/error/registry.py` maps to
`tests/error/test_registry.py`. Topic suffixes are supported, such as
`tests/error/test_openapi_http_responses.py`. Package-level tests use
`test_fastapi_canon.py`. Pytest uses importlib mode and mypy uses explicit package
bases so repeated module names do not require empty package-marker files.

Cross-feature acceptance tests and the retained transport suites are deliberately
unmirrored; their existing directory layouts are documented explicitly in the
policy. They still receive the test-quality checks. Tests need explicit outcomes,
nonempty generated-output evidence and bounded waits/processes. Collected async
tests assert their own results rather than hiding assertions in `asyncio.run`
helpers. HTTP test clients use context managers, and startup-failure tests exercise
the public lifespan context with a local deadline.

Private production access and string patch targets are errors. Focused error
contract tests use real collaborators. Other tests have a one-patch budget;
specific clock, cross-service telemetry and release-tool tests explain why they
need two independent seams. These are deliberate exceptions, not a general
increase of the budget.

## Deliberate exceptions

Static review cannot discover external application callers or prove that every
internal test seam should disappear. Supported application factories, adapter
APIs, track lifecycle methods and testing-library state retain specific reasons
beside their definitions. Dedicated flow-analysis, cancellation/accounting and
context-variable unit tests explain their direct internal access at the relevant
statement. Shared-session/report assertions explain why a small loop stays in a
single scenario.

Four existing factory/type-model cycles have narrowly located explanations:
telemetry configuration installs its native adapters; the SSE IR builder invokes
passes over its own model graph; WebRTC declarations and peer ports use mutual
forward types; RPC channel factories and serving middleware share service types.
Cycle checking remains active elsewhere. New suppressions require a concrete
reason, and stale suppressions fail the gate. Do not add a debt file or weaken
rules to bypass a finding; review and fix it or explain an intentional contract.

After upgrading Smelt, regenerate the schema using UTF-8 output and review policy
changes against the [upstream rule catalog](https://github.com/mathisarends/smelt/blob/v0.2.0/docs/rules/README.md).
