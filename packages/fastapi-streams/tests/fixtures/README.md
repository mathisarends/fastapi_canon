# Test fixtures

The committed OpenAPI documents are regression fixtures for the example apps and
client generators:

| Fixture | Source app |
| --- | --- |
| `agent_runs.openapi.json` | `examples.agent_runs:app` |
| `models.openapi.json` | `tests.fixtures.models_app:app` |

Run `uv run python scripts/codegen.py` with the locked dependencies to regenerate
these documents and their TypeScript/Python clients. `--check` detects drift.
Golden snapshot tests use the locked environment; dependency compatibility tests
export fresh contracts and generate fresh clients for the versions under test.

`integration_app.py` and `compat-client.ts` exercise live HTTP streams and cleanup.
