# Repository architecture

Canon is one Python library, built from the root `pyproject.toml`. Its import
package is `fastapi_canon/`; there is no Python or pnpm package workspace.
Optional transports and telemetry ship inside the same distribution and version.

```text
fastapi_canon/                  Library and public facades
  error/                       Error values, catalog and HTTP/OpenAPI adapters
  integrations/                SSE, WebSockets, WebRTC and telemetry
  codegen/                     Shared SDK planning and generation
  checks/                      Static error-contract analysis
  testing/                     Public application test support
tests/
  unit/                        Isolated behavior, including explicit test seams
  integration/                 Real framework and transport interactions
  contract/                    HTTP schemas, protocol contracts and artifacts
  fixtures/                    Contract snapshots and committed generated SDKs
  typescript/                  Browser SDK runtime and type checks
  browser/                     Real Chromium/WebRTC sessions
examples/                      Runnable applications and their generated clients
docs/                          Library, integration and contributor documentation
  integrations/                Detailed transport guides
  history/                     Predecessor changelogs and licenses
guides/                        Architecture conventions for consuming applications
scripts/                       Development, distribution and release entrypoints
tooling/                       Feature-specific static-analysis configurations
```

The root owns build metadata, dependency groups, `uv.lock`, the Node tooling
manifest and `pnpm-lock.yaml`. The reference service under
`examples/reference-service/` is an editable development dependency and an
independently installable example, not another Canon release target.
`release.toml` selects the repository root as the sole Canon release directory.

## Dependencies

Error values, metadata and rendering depend only on the standard library and
Pydantic. Successful-response metadata builds on those values. HTTP and OpenAPI
adapters consume the read-only `ErrorCatalog` protocol; routing builds on adapters,
and composition owns routing, Dishka and lifecycle integration. Each transport
has its own role and must not import sibling transports or eagerly load optional
engines. Generators may read protocol contracts. Repository scripts sit outside
the runtime package and may orchestrate its supported integrations.

The executable dependency map and source inventory live in `smelt.yml`. Read
[architecture guardrails](architecture-guardrails.md) for the individual rules,
exceptions and test-quality requirements, and [public namespaces](canon.md) for
consumer imports. Broader design decisions remain in
[`CANONICALIZATION_DECISIONS.md`](../CANONICALIZATION_DECISIONS.md); verify an API
exists in this checkout before using it.

## Tests and checks

Unit, integration and contract roots mirror library paths independently. For
example, `fastapi_canon/error/openapi.py` maps to
`tests/contract/error/test_openapi.py`; focused topics can use a descriptive suffix.
Existing protocol scenario suites remain explicitly listed in Smelt rather than
silently disabling mirroring for all tests. Integration and contract tests use
real collaborators and prohibit mocks. Shared snapshots live under `tests/fixtures/`.

All Python library tests run from the root in one pytest process using importlib
mode and qualified fixture/example imports. The reference service also has its
own installed-application test run. Feature-specific type/lint policies remain
in `tooling/`; this does not create separate package metadata or environments.
Generated files are updated through their generators and verified for drift.

Run `uv run --frozen smelt context <module-or-path>` before implementation, then
the checks in [contributing](contributing.md). Full Smelt coverage, errors and
warnings gate both CI and the root pre-commit hook. Wheel and sdist checks install
actual artifacts in fresh environments, without source-tree fallback.
