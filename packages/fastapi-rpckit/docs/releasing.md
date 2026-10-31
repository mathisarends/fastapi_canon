# Releasing Canon WebSockets

The WebSocket implementation now ships in the single `fastapi-canon`
distribution. This directory retains its feature-specific tests, examples,
guides and historical changelog. It is no longer a release target.

Follow the workspace [release policy](../../../docs/releases.md) and the
[package migration guide](../../fastapi-canon/docs/package-migration.md).
Use Canon's project version, `fastapi_canon/_version.py`, changelog and
`fastapi-canon-vX.Y.Z` tag. Regenerate clients through the Canon generators and
pass all feature suites plus installed-wheel checks before release.

The first predecessor-successor release was RPCKit 0.1.0, published on
2026-10-17. Existing published distributions remain available. New publications
require explicit user authorization and the root `release.yml` workflow.
