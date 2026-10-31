# Documentation

Define typed events in FastAPI, export their OpenAPI 3.2 contract, and generate
TypeScript Fetch and Python HTTPX clients from the same definitions.

| Guide | What you will find |
| --- | --- |
| [Quickstart](quickstart.md) | Run the complete example and define your first stream. |
| [Contracts and codegen](codegen.md) | Export OpenAPI, generate both clients, and detect drift in CI. |
| [Native transports](native-transports.md) | Existing SSE proxies, contract-first generation and private REST codegen views. |
| [Client usage](clients.md) | Named event values, typed iteration, authentication, cancellation, and errors. |
| [Development](development.md) | Local checks, test coverage, supported features, and roadmap. |
| [Releasing](releasing.md) | Workspace release preparation, PyPI publishing, and recovery. |
| [Architecture](architecture.md) | Server integration, shared IR, generation, and transport boundaries. |

Start with the [quickstart](quickstart.md), then use the
[codegen guide](codegen.md) to integrate the clients into your project.

Python 3.12+, FastAPI 0.143.3+, and Pydantic v2 are required. Code generation is
an optional extra: `fastapi-canon[sse,codegen]`. No running server is needed to
export contracts or generate clients.

The [example application](../examples/agent_runs/__init__.py) includes GET and
authenticated POST streams. Its [TypeScript](../examples/agent_runs/consume.ts)
and [Python](../examples/agent_runs/consume.py) consumers demonstrate the complete
journey. Generated clients and the [contract fixture](../tests/fixtures/agent_runs.openapi.json)
are committed so you can inspect their output.

[Back to the project](../README.md)
