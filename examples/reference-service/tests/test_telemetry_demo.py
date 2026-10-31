from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_canon.integrations.opentelemetry import Telemetry
from fastapi_canon.testing.telemetry import SpanCapture
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from starlette.types import Lifespan

pytest.importorskip("opentelemetry.instrumentation.httpx")
pytest.importorskip("opentelemetry.instrumentation.sqlalchemy")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

from opentelemetry.trace import SpanKind  # noqa: E402

import app.telemetry_demo as demo  # noqa: E402


async def test_demo_sql_and_cross_service_http_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()

    with SpanCapture() as tasks, SpanCapture() as notifier:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            yield

        def telemetry(name: str) -> tuple[Telemetry, Lifespan[FastAPI]]:
            capture = tasks if name == "tasks" else notifier
            return Telemetry(
                tracer_provider=capture.provider, logs=False, metrics=False
            ), lifespan

        monkeypatch.setattr(demo, "_telemetry", telemetry)
        notifier_app = demo.create_notifier_app()

        monkeypatch.setattr(
            demo,
            "_notifier_client",
            lambda: httpx2.AsyncClient(
                base_url="http://notifier",
                transport=httpx2.ASGITransport(app=notifier_app),
            ),
        )
        app = demo.create_tasks_app()
        with TestClient(notifier_app), TestClient(app) as client:
            client.headers["Authorization"] = "Bearer local-development-token"
            secret = "private-sql-parameter-8b73"
            response = client.post("/api/v1/tasks", json={"title": secret})
            assert response.status_code == 201
            repository = tasks.one("TaskRepository.save")
            assert repository.context is not None
            assert any(
                span.kind is SpanKind.CLIENT
                and span.parent is not None
                and span.parent.span_id == repository.context.span_id
                for span in tasks.spans
            )
            assert all(secret not in str(span.attributes) for span in tasks.spans)
            tasks.clear()

            assert client.post("/api/v1/demo/notify").json() == {"status": "sent"}
            gateway = tasks.one("NotificationGateway.send")
            remote = notifier.one("POST /notifications")
            outgoing = next(
                span for span in tasks.spans if span.kind is SpanKind.CLIENT
            )
            assert (
                gateway.context is not None
                and outgoing.context is not None
                and remote.context is not None
            )
            assert (
                outgoing.parent is not None
                and outgoing.parent.span_id == gateway.context.span_id
            )
            assert (
                remote.parent is not None
                and remote.parent.span_id == outgoing.context.span_id
            )
            assert (
                remote.context.trace_id
                == outgoing.context.trace_id
                == gateway.context.trace_id
            )
