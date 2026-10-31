# Reference service

A small task API demonstrating feature slices, repositories, mapping, transactions,
and authentication with the real `fastapi-canon` library. Features contribute
providers, `CanonRouter` declarations, and typed error registries. `Composition`
owns the Dishka container and its shutdown; the application has no local Feature
implementation or second container/lifespan manager.

The example defaults to SQLite for convenient local use. Persistent schemas should use Alembic; `create_schema.py` exists only to bootstrap this disposable example. See [Database migrations](../../guides/migrations.md) for the preferred layout.

```console
uv sync --locked --all-packages --all-groups
uv run python create_schema.py
uv run uvicorn app.main:app --reload
```

The task API is available under `/api/v1/tasks`; interactive documentation is at `/docs`. Task routes require `Authorization: Bearer local-development-token`. This token is a local example default only; deployed services inject real authentication configuration and a production `TokenVerifier` adapter.

Run the commands above from `examples/reference-service`. From the repository
root, use `uv run --locked --directory examples/reference-service pytest -q` for
the HTTP/authentication/contract regressions.

Errors now use Canon's RFC 9457 `application/problem+json` contracts. The stable
codes are `authentication_required`, `task_not_found`, and
`request_validation_error`; `WWW-Authenticate: Bearer` is preserved for 401.
Successful response schemas, paths, and operation IDs remain unchanged. The
versioned feature routes and the public `/health` endpoint are wired explicitly
at the composition root.

## Local telemetry demo

The normal application remains exporter-free. Its Dishka TaskProvider declares
`observe(TaskRepository)` and `observe(TaskService, expected=(TaskNotFound,))`;
these decorators return the original instances unless composition enables
telemetry. `create_app(telemetry=Telemetry(...))` accepts application-owned
providers. See the [developer API](../../packages/fastapi-canon/docs/telemetry.md).

For the opt-in demo, run from the repository root:

```console
uv sync --locked --all-packages --all-groups --all-extras
docker compose -f examples/reference-service/compose.telemetry.yaml up -d
uv run --locked --directory examples/reference-service python create_schema.py
```

Start these commands in two terminals:

```console
uv run --locked --directory examples/reference-service --extra otel uvicorn app.telemetry_demo:create_tasks_app --factory --port 8000
uv run --locked --directory examples/reference-service --extra otel uvicorn app.telemetry_demo:create_notifier_app --factory --port 8001
```

`POST http://localhost:8000/api/v1/demo/notify` produces a trace containing a
NotificationGateway port span, an outgoing HTTPX2 span and the notifier's native
server span. The normal task endpoints also show repository and SQL spans.
`/health` is excluded. Grafana is available at `http://localhost:3000`; select
Tempo in Explore and search the `tasks` or `notifier` service. The collector is
local development infrastructure using [Grafana's LGTM image](https://github.com/grafana/docker-otel-lgtm).

The demo owns an explicit SDK provider per service and shuts it down after
Dishka closes engines/clients. It exports traces only, uses parent-based sampling
at ratio 1.0 and never sets global providers. Override `NOTIFIER_URL` or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for other local ports. SQLAlchemy's contrib
instrumentor also wraps engine factories globally; run each demo application in
its own process. HTTPX2 instrumentation is scoped to the owned client.

Stop the collector with:

```console
docker compose -f examples/reference-service/compose.telemetry.yaml down
```
