from collections.abc import AsyncIterator
from typing import Literal

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi_canon.integrations.sse import StreamRouter
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import ContractError, export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from examples.agent_runs.errors import consume_with_errors
from examples.agent_runs.generated_python import Client, HttpError
from tests.codegen.helpers import event_document, load_package


class MissingResource(Exception):
    pass


class MissingProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal["missing"]
    status: Literal[404]
    title: Literal["Not found"]
    type: Literal["urn:example:missing"]


@pytest.mark.parametrize(
    ("body", "handled"),
    [
        (
            {"detail": [{"loc": ["run_id"], "msg": "Invalid", "type": "value_error"}]},
            True,
        ),
        ("<html>Bad gateway</html>", False),
        ([], False),
        ({"detail": [{}]}, False),
    ],
)
async def test_python_consumer_example_checks_bodies_and_keeps_http_fallback(
    body, handled
):
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(422, json=body))
        ) as http,
        Client("http://example.test", http_client=http) as client,
    ):
        if handled:
            await consume_with_errors(client, "demo")
        else:
            with pytest.raises(HttpError):
                await consume_with_errors(client, "demo")


async def test_standalone_http_problem_preserves_details_and_supports_body_aliases(
    tmp_path,
):
    app = _problem_app()
    contract = build_ir(export_schema(app))
    with load_package(tmp_path, generate_python(contract)) as sdk:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http,
            sdk.Client("http://example.test", http_client=http) as client,
        ):
            with pytest.raises(sdk.HttpError) as caught:
                _ = [item async for item in client.streams.events()]
        error = caught.value
        assert error.status == 404
        assert error.response.headers["content-type"] == "application/problem+json"
        body = TypeAdapter(sdk.models.EventsErrorBody).validate_python(error.details)
        assert isinstance(body, sdk.models.EventsError0)
        legacy = TypeAdapter(sdk.models.EventsError).validate_python(error.details)
        assert body.model_dump() == legacy.model_dump() == error.details
        assert body.code == "missing"
        for invalid in ("<html>Bad gateway</html>", None, {}, {"code": "missing"}):
            with pytest.raises(ValidationError):
                TypeAdapter(sdk.models.EventsErrorBody).validate_python(invalid)
    typescript = generate_typescript(contract)
    api = typescript["streams/api.ts"]
    assert "export type EventsErrorBody" in api
    assert "export type EventsError = EventsErrorBody;" in api
    assert "404 application/problem+json" in api
    assert "isHttpError" in typescript["index.ts"]


@pytest.mark.parametrize("language", ["typescript", "python"])
@pytest.mark.parametrize("suffix", ["Error", "ErrorBody"])
def test_public_error_alias_collisions_are_diagnosed(language, suffix):
    document = event_document(str)
    name = "Events" + suffix
    document["components"] = {"schemas": {name: {"type": "string"}}}
    operation = document["paths"]["/events"]["get"]
    data = operation["responses"]["200"]["content"]["text/event-stream"]["itemSchema"][
        "properties"
    ]["data"]
    data["contentSchema"] = {"$ref": f"#/components/schemas/{name}"}
    operation["responses"]["404"] = {
        "description": "Missing",
        "content": {"application/problem+json": {"schema": {"type": "string"}}},
    }
    render = generate_python if language == "python" else generate_typescript
    with pytest.raises(ContractError, match="collision"):
        render(build_ir(document))


def _problem_app():
    router = StreamRouter()

    def require_resource() -> None:
        raise MissingResource

    @router.stream(
        "/events",
        operation_id="events",
        dependencies=[Depends(require_resource)],
        responses={
            404: {
                "description": "Not found",
                "content": {
                    "application/problem+json": {
                        "schema": MissingProblem.model_json_schema()
                    }
                },
            }
        },
    )
    async def events() -> AsyncIterator[str]:
        yield "unreachable"

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(MissingResource)
    async def missing_response(
        _request: Request, _error: MissingResource
    ) -> JSONResponse:
        problem = MissingProblem(
            code="missing", status=404, title="Not found", type="urn:example:missing"
        )
        return JSONResponse(
            problem.model_dump(), status_code=404, media_type="application/problem+json"
        )

    return app
