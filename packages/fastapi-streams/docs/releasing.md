# Releasing fastapi-streams

Use the [workspace release process](../../../docs/releases.md). Only root
workflows execute in this repository. Version metadata and release validation
come from the root `release.toml` and `scripts/release.py`.

Releases use independent `fastapi-streams-vX.Y.Z` tags and the root lockfile.
Prepare a new version, update the changelog and generated clients, pass the
workspace and installed-artifact checks, and wait for CI on the exact commit.
Run the root release workflow with `dry_run=true` before publication. A real
publication requires explicit authorization.

[Back to documentation](README.md)
