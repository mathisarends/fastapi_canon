# uv workspace

The prepared Canon 0.9.0 source has one publishable workspace member,
`packages/fastapi-canon`, plus the development-only reference service. The former
transport package directories retain tests/tooling and are not workspace members.
All transport implementations ship in the Canon wheel.

Use a uv workspace when one repository contains multiple genuine Python distributions that belong together and need local package resolution. A backend, an agent runtime, an LLM integration package, and a standalone token utility are plausible members. Domain, application, infrastructure, presentation, and individual feature slices of one service are not.

Prefer flat, named members at the repository root, as in `backend/`, `agent/`, `llm/`, and `tokens/`. Do not introduce generic `services/` and `packages/` directories merely to classify workspace members. Add an intermediate grouping only when the repository has enough members that the grouping conveys durable ownership or lifecycle information.

The Canon repository keeps its library source in `packages/fastapi-canon`
and demonstration applications in `examples/`. The former transport package
directories retain tests, guides and tooling during consolidation; they are not
independently published members. The root is a non-published coordination
project. See the [workspace inventory](../README.md#workspace).

The root owns workspace membership and the single shared `uv.lock`. Each member
owns its runtime dependencies, optional command entry points, and build metadata.
Prefer a `src/` package for new projects and match directory, distribution, and
import names where practical. Existing libraries may keep their established flat
layout when clean installed-artifact tests verify imports without source-tree
fallbacks. The imported reference service retains its `src/app` namespace; new
application members should use an application-specific import name.

Workspace membership does not imply a dependency. A member declares another member only when its runtime code imports that package, and the local source is explicitly resolved from the workspace. Keep the member dependency graph directed and acyclic; sharing a repository is not justification for reciprocal imports.

Extract a separate member when the code has a coherent public API and is consumed independently by another process or package. Do not create a vaguely named `shared`, `common`, or `utils` member as a destination for unrelated helpers. If code is used by only one deployable application and has no independent package boundary, keep it inside that application.

Non-Python applications may live beside workspace members without becoming uv members. For example, a frontend remains a normal root-level project while Python members continue to share the uv lockfile.

Keep repository-wide topology such as `compose.yml` at the root. Put a Dockerfile beside each independently built member. For the container conventions, see [Docker](docker.md).
