# Canon workspace

Read `CANONICALIZATION_DECISIONS.md` for the agreed target architecture and
implementation scope. Canonical error registration, SSE/RPC bindings, and generated-client handling
are documented in `packages/fastapi-canon/docs/problems.md`. The common `fastapi-canon generate` workflow, domain facade and shared model/error
runtime are implemented; see `docs/unified-canon-concept.md`. Verify implemented behavior against the source.

Use `README.md` for the package inventory, `docs/contributing.md` for development
commands and `docs/compatibility.md` for the tested dependency scope. Package
guides document implemented APIs. `docs/canon.md` defines the canonical package
and public namespace boundaries; `docs/unified-canon-concept.md` documents the
implemented version-1 codegen configuration and its explicit limits.
Write all repository Markdown documentation in English, including changelogs,
guides, and agent instructions.
The canonical development and future release repository is
`mathisarends/fastapi_canon`. Follow `docs/repository-migration.md` for the cutover;
preserve historical release evidence in `mathisarends/canon`.

## Boundaries

- `packages/fastapi-canon`: the only active release target, including composition,
  Dishka lifecycle, HTTP contracts, `sse`, `websockets`, `webrtc` and their generators.
  Core and CLI help must run without optional codegen, telemetry or engines.
- Keep all optional integrations under `fastapi_canon.integrations`: `sse`,
  `websockets`, `webrtc` and `opentelemetry`. Core HTTP/composition stays at the
  package root. WebSocket framework adapters live in `.websockets.adapters`.
- `packages/fastapi-streams` and `packages/fastapi-rpckit`: retained SSE and
  WebSocket test/example/tooling directories. Preserve wire behavior and clients.
- `packages/fastapi-webrtc`: retained WebRTC tests, examples and tooling for
  signaling protocol `fastapi-webrtc.v1`. Browser tests
  run with `uv run --locked python scripts/check.py browser` after
  `npx playwright install chromium` in that package.
- Transport modules must not import each other or load optional engines eagerly.
- Use one Canon version. Add shared abstractions only for concrete reuse.
- Application code follows `guides/architecture.md`: feature slices, inward
  dependencies, framework-free domain, Dishka for application dependencies.

## Workflow

Use the root environment and lockfile:

```console
uv sync --locked --all-packages --all-groups
npm ci --ignore-scripts --prefix packages/fastapi-streams
npm ci --ignore-scripts --prefix packages/fastapi-webrtc
uv run --locked python scripts/check.py all
uv run --locked python scripts/check_distributions.py
```

`check.py` also accepts `architecture`, `lint`, `types`, `test`, `clients`, and
`browser`. It runs each
package with its own configuration and working directory. Do not combine all
package suites into one pytest process: their `tests`/`examples` modules collide.
For a focused feature suite, run `uv run --locked --project ../.. python -m pytest ...`
from its retained test directory.

The root `smelt.yml` defines strict architecture, source and test policies; see
`docs/architecture-guardrails.md`. Before editing, run
`uv run --locked smelt context <module-or-path>`. After editing, run
`uv run --locked smelt check --config smelt.yml --changed --fail-on warning` and
fix findings. `scripts/check.py architecture` and `scripts/check.py all` run the
full check, as does the root pre-commit hook. Warnings fail the gate. Use
`smelt explain <rule-or-code>` when unsure; do not introduce a debt baseline or
weaken rules merely to pass. Keep any deliberate exception narrowly located and
give its concrete reason; unused suppressions fail.

Keep one root `uv.lock`. Preserve package-specific type/lint settings. Generated
clients are changed through generators/templates, then checked for drift.
Build/install checks must use actual artifacts in fresh environments, independent
of workspace source overrides. Do not publish or archive repositories as part of
routine development. Only root workflows under `.github/workflows/` define CI and releases.

## Release preparation

Before 1.0, every breaking change requires the next minor release:
`0.x.y` becomes `0.(x+1).0`. Patch releases are only for backward-compatible
fixes and documentation. Count public API/signature changes, stricter accepted
inputs or validation, changed defaults and generated-client incompatibilities
as breaking changes, even when the wire format stays unchanged. Record them
under a `### Breaking changes` changelog heading with concrete migration steps.
Use the Canon version; do not classify a breaking change as a patch merely
because its implementation is small. Release validation checks declared breaking
changelog sections against this policy.

Read `docs/releases.md` for prepared versions, publication order, publisher setup
and the release workflow. `docs/compatibility.md` records tested dependency
combinations; `docs/rpc-parity.md` records the predecessor comparison and consumer
migration limits. Do not treat prepared source versions as published releases.

Run the standalone minimum-dependency smoke matrix with
`uv run --locked python scripts/check_distributions.py --python 3.12 --dependencies minimum`.
The default mode checks current compatible dependencies. Both modes create fresh
wheel environments and write dependency inventories under `dist/`. Check Streams'
installed CLI/backends with `uv run --locked python scripts/check_streams_artifacts.py`
and WebRTC's installed CLI and generated TypeScript client with
`uv run --locked python scripts/check_webrtc_artifacts.py` (after
`npm ci --ignore-scripts --prefix packages/fastapi-webrtc`).
For an external RPC contract, follow the installed-wheel command in the parity
report; SDK compilation alone does not establish a full application migration.

Use `release.toml` and `scripts/release.py` for package/tag/version metadata.
Prepare source versions and pins, regenerate clients, pass checks and commit/merge
to `main`. Wait for CI on that exact commit, then dispatch `release.yml` with
`dry_run=true` for the selected package/version. Before a real publication,
finalize the changelog date and pass CI for the final source. Only dispatch with
`dry_run=false` when explicitly authorized to publish. Verify public-index installs and release/tag results.
On failure, follow the documented resume procedure with the original run's
verified artifacts; do not replace published contents or move existing tags.
After verified publication, update the README's current-release table and the
release/compatibility records. All workspace package releases belong in this
repository's central GitHub Releases list, under the `fastapi-canon-v` tag prefix.
Always create the matching GitHub Release for every package publication, including
changelog notes, the verified wheel/sdist and the release manifest. A publication
is not complete until its PyPI install, tag, GitHub Release and attached artifacts
have been verified. Do not treat a PyPI-only upload as a completed release.
