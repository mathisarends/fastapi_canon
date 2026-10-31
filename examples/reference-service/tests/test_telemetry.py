from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi_canon.integrations.opentelemetry import Telemetry
from fastapi_canon.testing.telemetry import SpanCapture
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import app.features.tasks.infrastructure.models  # noqa: F401
from app.main import create_app

pytest.importorskip("opentelemetry.sdk")


@pytest.mark.asyncio
async def test_endpoint_trace_contracts_and_payload_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'telemetry.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()

    with SpanCapture() as capture:
        app = create_app(
            telemetry=Telemetry(tracer_provider=capture.provider, logs=False)
        )
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer local-development-token"
            assert client.get("/health").status_code == 200
            capture.assert_tree(
                "GET /health",
                ["fastapi.dependencies", "fastapi.endpoint", "fastapi.serialization"],
            )
            capture.clear()

            secret = "private-payload-7a80c0"
            response = client.post("/api/v1/tasks", json={"title": secret})
            assert response.status_code == 201
            key = response.json()["id"]
            capture.assert_tree(
                "POST /api/v1/tasks",
                [
                    "fastapi.dependencies",
                    (
                        "fastapi.endpoint",
                        [("TaskService.create", ["TaskRepository.save"])],
                    ),
                    "fastapi.serialization",
                ],
            )
            assert all(
                secret not in str(span.attributes) + str(span.events)
                for span in capture.spans
            )
            capture.clear()

            assert client.get("/api/v1/tasks").status_code == 200
            capture.assert_tree(
                "GET /api/v1/tasks",
                [
                    "fastapi.dependencies",
                    (
                        "fastapi.endpoint",
                        [("TaskService.list_recent", ["TaskRepository.list_recent"])],
                    ),
                    "fastapi.serialization",
                ],
            )
            capture.clear()

            assert client.post(f"/api/v1/tasks/{key}/completion").status_code == 200
            capture.assert_tree(
                "POST /api/v1/tasks/{task_id}/completion",
                [
                    "fastapi.dependencies",
                    (
                        "fastapi.endpoint",
                        [
                            (
                                "TaskService.complete",
                                ["TaskRepository.get_by_id", "TaskRepository.save"],
                            )
                        ],
                    ),
                    "fastapi.serialization",
                ],
            )
            capture.clear()

            assert client.post(f"/api/v1/tasks/{uuid4()}/completion").status_code == 404
            span = capture.one("TaskService.complete")
            assert span.status.status_code.name == "UNSET"
            assert span.events[0].name == "canon.port.expected_exception"
            server = capture.one("POST /api/v1/tasks/{task_id}/completion")
            assert server.attributes is not None
            assert server.attributes["canon.problem.code"] == "task_not_found"
            assert "error.type" not in server.attributes
