from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import app.features.tasks.infrastructure.models  # noqa: F401
from app.main import create_app


@pytest.fixture
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer local-development-token"},
        ) as test_client:
            yield test_client

    await engine.dispose()


async def test_task_http_flow(client: AsyncClient) -> None:
    health = await client.get("/health")
    assert health.json() == {"status": "ok"}

    created = await client.post("/api/v1/tasks", json={"title": "Ship the canon"})
    assert created.status_code == 201

    task_id = created.json()["id"]
    completed = await client.post(f"/api/v1/tasks/{task_id}/completion")
    assert completed.status_code == 200
    assert completed.json()["completed"] is True

    listed = await client.get("/api/v1/tasks")
    assert listed.status_code == 200
    assert [task["id"] for task in listed.json()["items"]] == [task_id]

    openapi = (await client.get("/openapi.json")).json()
    operation_ids = {
        operation["operationId"]
        for path in openapi["paths"].values()
        for operation in path.values()
    }
    assert operation_ids == {
        "complete_task",
        "create_task",
        "get_health",
        "list_tasks",
    }


@pytest.mark.parametrize("authorization", ["", "Bearer invalid-token"])
async def test_tasks_require_authentication(
    client: AsyncClient, authorization: str
) -> None:
    response = await client.get(
        "/api/v1/tasks",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "authentication_required"
    assert (
        problem["type"] == "https://api.example.test/problems/authentication_required"
    )
    assert problem["detail"] == "Authentication required"
    assert "invalid-token" not in response.text


async def test_missing_task_has_the_documented_problem_contract(
    client: AsyncClient,
) -> None:
    response = await client.post(f"/api/v1/tasks/{uuid4()}/completion")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "task_not_found"
    assert problem["detail"] == "The requested task does not exist."
    schema = (await client.get("/openapi.json")).json()
    completion = schema["paths"]["/api/v1/tasks/{task_id}/completion"]["post"]
    documented = completion["responses"]["404"]["content"]["application/problem+json"]
    assert documented["schema"]["$ref"] == "#/components/schemas/TaskNotFoundProblem"
    auth = completion["responses"]["401"]
    assert "WWW-Authenticate" in auth["headers"]
    assert auth["content"]["application/problem+json"]["schema"]["$ref"] == (
        "#/components/schemas/AuthenticationRequiredProblem"
    )


async def test_validation_uses_problem_details(client: AsyncClient) -> None:
    response = await client.post("/api/v1/tasks", json={"title": ""})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "request_validation_error"
