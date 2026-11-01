# Canon releases

Release the unified `fastapi-canon` distribution from `mathisarends/fastapi_canon`. The workspace
and reference service are not publication targets. `release.toml` maps package
names, directories, modules and tag prefixes; `scripts/release.py` validates them.
Published versions and their GitHub/PyPI assets are listed in the
[README](../README.md#current-releases). Package changelogs contain migration notes;
GitHub Releases and Actions retain publication evidence.
The [repository migration](repository-migration.md) records the cutover. The
historical runs, tags and assets linked below remain in their original repositories.

## Current versions

The source tree prepares Canon **0.11.0 (unreleased)**. This is the next minor
release after 0.10.0 because `observe()` is removed in favor of feature-owned
`instrument()` declarations. It also includes the generated Python RPC client
boundary fix, shared pnpm tooling and the publisher verification setup fix.
See the [0.11.0 changelog](../packages/fastapi-canon/CHANGELOG.md#0110---2026-11-01)
for migration steps, including client regeneration. Package metadata, the shared
version constant, root pin, reference-service range and generated manifests use
0.11.0. Validate the prepared source with:

```console
uv run --locked python scripts/release.py validate --package fastapi-canon --version 0.11.0
```

Before publication, finalize the changelog date, merge the prepared source to
`main`, pass exact-source CI and dispatch the 0.11.0 dry run as described below.
The published release below remains unchanged until publication is verified.

Canon 0.10.0 was published on 2026-11-01 from
[verified source `4d9ce18`](https://github.com/mathisarends/fastapi_canon/commit/4d9ce1842b350a9bf867d512cc89aafc4b44d53b),
after [exact-source CI](https://github.com/mathisarends/fastapi_canon/actions/runs/41311732023)
and the [final dry run](https://github.com/mathisarends/fastapi_canon/actions/runs/41312304534).
It includes the unpublished 0.9.0 consolidation and its migrations; 0.9.0 was
never published separately. The
[publication run](https://github.com/mathisarends/fastapi_canon/actions/runs/41312590718)
uploaded its original verified wheel and sdist after correcting the Trusted
Publisher's repository/environment fields. Its public-install check then failed
because the publish job had not installed the npm compiler dependencies.

Recovery kept the original source, notes, manifest and artifact bytes. The
documented `verify-pypi` command passed all six public-index installation
combinations locally on Windows/Python 3.14; the combined extras also passed a
fresh Windows/Python 3.12 public-index installation. The documented `finish`
command created the matching
[tag and GitHub Release](https://github.com/mathisarends/fastapi_canon/releases/tag/fastapi-canon-v0.10.0).
Downloaded GitHub assets, PyPI file hashes and the original manifest agree, and
the tag points to the verified source. The publish job now installs Node.js and
the shared pnpm TypeScript tooling before public-install verification.

Streams 0.5.0, Canon 0.8.1 and RPCKit 0.4.1 were published on 2026-10-25 from
[verified source `ebd725b`](https://github.com/mathisarends/canon/commit/ebd725b14610f4017614281d9ad074920cf6f002),
after [exact-source CI](https://github.com/mathisarends/canon/actions/runs/40237725498). Streams was published
before Canon, which pins it. Publication workflows:
[Streams](https://github.com/mathisarends/canon/actions/runs/40238129418),
[Canon](https://github.com/mathisarends/canon/actions/runs/40238523107) and
[RPCKit](https://github.com/mathisarends/canon/actions/runs/40238531843). The RPCKit finalization job was rerun
once on the original run after the PyPI API briefly reported an incomplete
upload; no files were replaced. PyPI hashes match the GitHub Release assets and
manifests, each tag points to the verified source, and a fresh Windows/Python 3.12
public-index installation of all four current package versions passed import and
CLI smoke checks.

Before that, Canon 0.8.0 was published on 2026-10-25 from
[verified source `50b8e4b`](https://github.com/mathisarends/canon/commit/50b8e4b442b93ad75b8e2b36024487e38f308330),
after [exact-source CI](https://github.com/mathisarends/canon/actions/runs/40213500982)
and the [publication workflow](https://github.com/mathisarends/canon/actions/runs/40214446851).

Previously, WebRTC 0.3.0 and Canon 0.7.0 were published on 2026-10-24 from
[verified source `6aad274`](https://github.com/mathisarends/canon/commit/6aad274800d1960a8bbbcb0fd7d54ec3f6901c99),
in that order. Both publication workflows completed successfully:
[WebRTC](https://github.com/mathisarends/canon/actions/runs/40079433426) and
[Canon](https://github.com/mathisarends/canon/actions/runs/40079766254).
Each has a matching tag and GitHub Release with its original wheel, sdist and
manifest. PyPI file hashes, public installs, release notes and downloaded GitHub
assets are verified.

| Package | Version | Migration |
| --- | --- | --- |
| Canon | 0.10.0 | Replace the separate transport distributions with Canon extras, update imports/CLI paths, and regenerate clients. Follow the [0.10.0 notes](../packages/fastapi-canon/CHANGELOG.md#0100---2026-11-01) and included [0.9.0 migrations](../packages/fastapi-canon/CHANGELOG.md#090---unreleased). |
| WebRTC | 0.3.0 | Use public enum members, keyword-only engine/channel options and `DescriptionType`; handle lazy type aliases in introspection tools. See the [migration notes](../packages/fastapi-webrtc/CHANGELOG.md#030---2026-10-24). |
| Streams | 0.5.0 | Regenerate clients. SDK-owned generated Python clients bound the connect time to 10 seconds; pass `RequestOptions(timeout=None)` or inject `httpx.AsyncClient(timeout=None)` to keep it unbounded. See the [Streams 0.5.0 notes](../packages/fastapi-streams/CHANGELOG.md#050---2026-10-25). |
| RPCKit | 0.4.1 | None; wire output and generated clients are unchanged apart from the manifest generator version. See the [RPCKit 0.4.1 notes](../packages/fastapi-rpckit/CHANGELOG.md#041---2026-10-25). |

The earlier Canon 0.8.1 and transport publications used separate distributions.
Their consolidation ships in Canon 0.10.0 without internal distribution pins.

### Consumer integration upgrade

The 2026-10-20 consumer integration batch addressed feedback for Canon 0.5.5,
WebRTC 0.1.0 and RPCKit 0.3.0:

- [Canon admission errors](../packages/fastapi-canon/docs/webrtc.md) reuse named
  errors, validate registry membership and export declared dependency/factory
  failures in session contracts. HTTP denials happen before peer allocation;
  accepted sessions retain WebRTC protocol errors.
- [Remote engine bindings and leases](../packages/fastapi-webrtc/examples/remote_video/README.md)
  keep each viewer's resources independent, including cancellation and rejected
  second viewers. The example documents native error mapping and partial stats.
- [Browser video health](../packages/fastapi-webrtc/docs/media-health.md) observes
  decoded or compositor frames with optional first-frame and stall deadlines.
  Applications retain control of playback and fallback. The optional
  [HTTP ticket recipe](../packages/fastapi-webrtc/docs/admission-tickets.md) provides
  readable admission errors before signaling.
- [RPCKit factory typing](../packages/fastapi-rpckit/CHANGELOG.md#040---2026-10-20)
  removes the URL-parameter and backend subprotocol mismatches. Regenerate clients
  before removing consumer generator overrides.

The signaling protocol stays `fastapi-webrtc.v1`. A maintained CDP binding and
deployed TURN/NAT, database concurrency and proxy validation remain separate work.

## Unified release scope

Canon 0.10.0 includes the unpublished 0.9.0 package consolidation and optional
native OpenTelemetry integration, plus canonical error bindings and common SDK
generation. See the [migration guide](../packages/fastapi-canon/docs/package-migration.md).
Only Canon is an active release target; the old feature directories retain
tests, examples and historical changelogs. Source metadata alone does not establish
a publication; the verified 0.10.0 publication is recorded above.

## Publisher setup

The required Trusted Publisher uses GitHub owner `mathisarends`, repository
`fastapi_canon`, workflow filename `release.yml` and environment `pypi`. Configure
it on the existing PyPI `fastapi-canon` project before publishing from this
repository; source changes do not update PyPI account settings. The GitHub
environment permits `main`. See the [cutover checklist](repository-migration.md#cutover).
Publishing uses job-scoped OIDC; no persistent PyPI token is needed. Only root
workflows are active. Every real publication requires explicit user authorization.

## Prepare a version

Before 1.0, breaking changes advance `0.x.y` to `0.(x+1).0`. Patch versions are
for backward-compatible fixes and documentation. Public signatures, accepted
inputs, stricter validation, defaults and generated-client contracts count even
when wire formats do not change. Use a `### Breaking changes` changelog heading
with concrete migration steps. Release validation enforces the next minor and
patch zero for declared breaking changes.

1. Update the package version, changelog and existing `__version__` constant.
   Canon and its feature modules share `fastapi_canon/_version.py`.
2. Update root pins, Canon's feature dependency ranges and the
   reference service's Canon range as needed. Run `uv lock` at the root.
3. Regenerate clients through their generators. Run the workspace, browser,
   current/minimum wheel and installed CLI checks in [contributing](contributing.md).
4. Commit/merge to `main` and wait for the complete `ci.yml` push run on that exact
   commit. Then dispatch the selected package's dry run.
5. Before an authorized publication, finalize the changelog heading as
   `## VERSION - YYYY-MM-DD`, commit/push and pass exact-source CI again.

Local metadata validation does not establish remote publication readiness:

```console
uv run --locked python scripts/release.py validate --package fastapi-canon --version NEXT_VERSION
```

## Dispatch a dry run

```console
gh workflow run release.yml --repo mathisarends/fastapi_canon --ref main -f package=fastapi-canon -f version=NEXT_VERSION -f dry_run=true
gh run list --repo mathisarends/fastapi_canon --workflow release.yml --limit 10
```

Choose the package and its prepared, unpublished version. Use the workflow filename
without `.github/workflows/`. Canonical prereleases are supported; local/dev/post
versions are not. A new run rejects a version already on PyPI.

Preparation verifies metadata, constants, pins, changelog, lockfile, main ancestry,
exact-commit CI, tags and PyPI state. Only the selected wheel/sdist enters
`dist/release/`; other packages are built as test dependencies. Fresh environments
exercise those upload artifacts with current dependencies and direct minima on
Python 3.12, plus installed Streams/WebRTC CLI and generated-client checks.

Each run uploads `release-PACKAGE-VERSION` containing `release/` and
`release-report/`: the package/version/commit/tag, CI/run links, check results,
SHA-256 hashes and changelog-derived notes. Notes include the selected version and
consecutive older sections marked `unreleased`, stopping at the first published
section. Thus the 0.10.0 notes carry the 0.9.0 migrations when upgrading from 0.8.1,
even after the 0.10.0 changelog date is finalized. Relative documentation links
resolve against the verified source commit. Artifacts are retained for 90 days.
Dry runs have neither OIDC nor repository write permissions and skip publication
and finalization. Preparation artifacts can be replaced on a preparation retry;
publication uses the successful run's original verified bytes.

## Publish when explicitly authorized

After final changelog dates and exact-source CI, dispatch the selected package
with `dry_run=false` only when publication is explicitly authorized:

```console
gh workflow run release.yml --repo mathisarends/fastapi_canon --ref main -f package=fastapi-canon -f version=NEXT_VERSION -f dry_run=false
```

The `publish` job downloads and verifies the prepared artifacts and uploads only
the wheel/sdist from a separate staging directory. Signature sidecars created by
the publisher must not modify the canonical artifact directory. Existing PyPI
files must match the original manifest; conflicting contents fail. Canon also
contains all transport modules and their generator resources in one artifact set.

After upload, verify hashes and fresh public-index installs. Canon checks core,
`[otel]`, `[sse]`, `[websockets]`, `[webrtc]` and combined feature/tool extras.
The separate `finish` job then
creates the package-prefixed tag and central GitHub Release at the verified
commit, with changelog notes, original wheel/sdist and manifest attached.
Prereleases produce prerelease GitHub Releases. A PyPI-only upload is incomplete.

## Resume and record the outcome

- A source fix requires a new commit, exact-source CI and a new run. Retry a
  transient failure on unchanged source with
  `gh run rerun RUN_ID --failed --repo mathisarends/fastapi_canon`.
  Historical publication runs must be resumed in the repository recorded by
  their original manifest, including `mathisarends/canon` for its past releases.
- After upload/finalization failure, rerun only failed jobs on the original run.
  Partial uploads send missing original files; complete uploads are verified
  without reuploading. Never rebuild under a published version or move a tag.
- Matching releases are accepted on retry; a tag at another commit is rejected.
- Keep root workflows unchanged until a release finishes. If they change, an
  older source's release can require an already authorized CLI login with
  workflow-write permission. Use the manifest's exact commit, original notes,
  wheel, sdist and report, then retry finalization. See
  [GitHub's release API permission note](https://docs.github.com/en/rest/releases/releases#create-a-release).
- After verified publication, update the README's current versions and
  compatibility table. Keep per-run logs, manifests and checksums in Actions and
  the matching GitHub Release rather than adding development transcripts here.
