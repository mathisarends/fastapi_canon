# Repository migration

## Scope

The unified Canon source returns from `mathisarends/canon` to
[`mathisarends/fastapi_canon`](https://github.com/mathisarends/fastapi_canon).
That repository becomes the single home for development, issues, CI and future
releases. The distribution remains `fastapi-canon`; public Python imports remain
`fastapi_canon`, including `fastapi_canon.integrations.*`.

This migration prepares source and release configuration. It does not publish
0.9.0, change existing PyPI artifacts, modify account-level publisher settings,
or implement the [codegen discussion](unified-canon-concept.md).

## Source and history

The imported source is `canon/main` at
[`64b9aca`](https://github.com/mathisarends/canon/commit/64b9aca2798c8f0359351d896ea2953e25a5e19e),
including the [single-distribution consolidation](https://github.com/mathisarends/canon/pull/18).
The former standalone `fastapi_canon/main` at
[`f275d31`](https://github.com/mathisarends/fastapi_canon/commit/f275d3129671296bc9565155e641488e77d42074)
is already an ancestor of that source. The migration branch keeps both histories,
including the SSE, WebSocket and WebRTC imports, without rewriting commits.

Merge the migration PR using a **merge commit** so all imported commits remain
reachable from `main`. Do not squash or rebase this migration. Only the migration
branch is pushed; existing branches and release tags are not replaced.

The layout remains unchanged: `packages/fastapi-canon` is the only library build
target; other feature directories retain tests, examples and tooling; architecture
guides and the development-only reference service remain in the workspace. Use
the root lockfile and [contributing commands](contributing.md). A fresh clone
avoids retaining the former standalone checkout's environment and file layout:

```console
git clone https://github.com/mathisarends/fastapi_canon.git
cd fastapi_canon
uv sync --locked --all-packages --all-groups
```

## Release evidence

| Releases | Evidence location |
| --- | --- |
| Original standalone Canon through 0.4.0 | Existing [`fastapi_canon` releases](https://github.com/mathisarends/fastapi_canon/releases), including the original `v0.4.0` tag |
| Canon 0.5.0 through 0.8.1 and the separately published transports | Original [`canon` releases](https://github.com/mathisarends/canon/releases), tags, workflow runs and attached artifacts |
| Unified Canon releases from 0.10.0 onward | [`fastapi_canon` releases](https://github.com/mathisarends/fastapi_canon/releases), using `fastapi-canon-v` tags |

Historical release links, manifests, checksums, assets and PyPI files stay at their
original locations. The migration does not recreate or move existing tags or
pretend that earlier publication runs happened in the new home. Resume an earlier
publication using its original repository, run, verified artifacts and manifest.
Published package metadata remains immutable; new repository links reach PyPI
with the next authorized release.

## Cutover

1. Pass the migration PR's root CI in `fastapi_canon`, then merge it with a merge
   commit. Wait for the full `ci.yml` push run on that exact `main` commit.
2. Configure the repository's Actions policies and required checks for the root
   workflow. GitHub settings, issues and open PRs are not transferred by a Git push.
   Existing discussions and release evidence remain accessible in `canon`.
3. Create or verify the GitHub environment `pypi` in `fastapi_canon`, permitting
   releases from `main`. Repository environments and protection rules are not
   copied with source files.
4. In the existing PyPI **fastapi-canon** project's Publishing settings, add or
   verify this Trusted Publisher:

   | Field | Value |
   | --- | --- |
   | Owner | `mathisarends` |
   | Repository | `fastapi_canon` |
   | Workflow filename | `release.yml` |
   | Environment | `pypi` |

   Follow [PyPI's existing-project setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).
   Do not create another PyPI project or add a persistent upload token.
5. After exact-source CI succeeds, dispatch the
   [documented dry run](releases.md#dispatch-a-dry-run) from the new repository.
   A dry run verifies source, build and installation checks; it does not test an
   OIDC exchange with PyPI. Verify the publisher fields separately.
6. Merge the companion `canon` notice PR after the target source is on `main`.
   The former home directs contributors here and retires its new-release dispatch
   workflow while keeping history, tags, releases and artifacts available. Remove
   the former `canon` Trusted Publisher when no authorized publication recovery
   still depends on it. Do not set up ongoing synchronization.
7. Publish only after separate explicit authorization, finalizing the changelog
   and passing exact-source CI as described in [releases](releases.md). Verify
   PyPI installs, the new tag, GitHub Release and attached artifacts before
   recording publication as complete.

Repository configuration in this PR does not prove that steps 2–5 have been
performed in GitHub or PyPI. Record their verification when executing the cutover.

Canon 0.10.0 became the first unified publication from this repository on
2026-11-01. The `pypi` GitHub environment restricts deployment to `main`, and the
corrected Trusted Publisher completed the OIDC exchange and PyPI upload. The
[release record](releases.md#current-versions) documents verification and recovery
of the original artifacts, including the matching GitHub Release.
