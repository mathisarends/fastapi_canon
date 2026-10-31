import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from fastapi_canon.integrations.sse import StreamRouter
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import BaseModel, Field, TypeAdapter

from examples.agent_runs import app
from tests.codegen.helpers import load_models


class Event(BaseModel):
    value: str


class ContractEvent(BaseModel):
    public: str = Field(alias="external-value")
    secret: str
    nullable: str | None


type UntypedEvent = Any


def test_get_post_validation_auth_and_serialization():
    with TestClient(app) as client:
        response = client.get("/v1/runs/abc/events?delay=0")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"run_id":"abc"' in response.text
        assert "世界" in response.text
        assert response.headers["x-accel-buffering"] == "no"
        assert client.get("/v1/runs/abc/events?delay=-1").status_code == 422
        url = "/v1/runs/abc/events"
        assert client.post(url, json={"prompt": "Hello"}).status_code in (401, 403)
        headers = {"Authorization": "Bearer demo-token"}
        response = client.post(url, json={"prompt": "Hi there"}, headers=headers)
        assert response.status_code == 200
        assert '"text":"there"' in response.text
        assert client.post(url, json={"prompt": ""}, headers=headers).status_code == 422
        assert client.get("/health").json() == {"status": "ok"}


def test_nested_routers_dependencies_headers_and_cleanup():
    lifecycle = []

    def resource() -> Iterator[str]:
        lifecycle.append("open")
        try:
            yield "resource"
        finally:
            lifecycle.append("closed")

    def authorize(x_key: Annotated[str, Header()]) -> None:
        if x_key != "secret":
            raise HTTPException(403, "Denied")

    router = StreamRouter(prefix="/inner", dependencies=[Depends(authorize)])

    @router.stream("/{name}", operation_id="nested")
    async def events(
        name: str,
        resource: Annotated[str, Depends(resource)],
    ) -> AsyncIterator[Event]:
        assert lifecycle == ["open"]
        yield Event(value=f"{name}:{resource}")

    parent = APIRouter(prefix="/parent")
    parent.include_router(router)
    application = FastAPI()
    application.include_router(parent, prefix="/v1")
    with TestClient(application) as client:
        assert (
            client.get("/v1/parent/inner/a", headers={"x-key": "bad"}).status_code
            == 403
        )
        assert lifecycle == []
        response = client.get("/v1/parent/inner/a", headers={"x-key": "secret"})
        assert '"value":"a:resource"' in response.text
        assert lifecycle == ["open", "closed"]


def test_sync_generator():
    router = StreamRouter()

    @router.stream("/sync")
    def events() -> Iterator[Event]:
        yield Event(value="sync")

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert '"value":"sync"' in client.get("/sync").text


def test_invalid_annotations_and_options_fail_at_registration():
    router = StreamRouter()

    async def untyped():
        yield {}

    async def ambiguous() -> AsyncIterator[Any]:
        yield {}

    async def aliased_any() -> AsyncIterator[UntypedEvent]:
        yield {}

    async def coroutine() -> Event:
        return Event(value="bad")

    for endpoint in (untyped, ambiguous, aliased_any, coroutine):
        with pytest.raises(TypeError):
            router.stream("/bad")(endpoint)
    with pytest.raises(ValueError, match="GET and POST"):
        router.stream("/bad", methods=["DELETE"])
    with pytest.raises(ValueError, match="GET and POST"):
        router.stream("/bad", methods=[])
    with pytest.raises(TypeError, match="infer"):
        router.stream("/bad", response_model=Event)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("response_model_include", {"public"}),
        ("response_model_include", set()),
        ("response_model_exclude", {"secret"}),
        ("response_model_by_alias", False),
        ("response_model_exclude_none", True),
        ("response_model_exclude_unset", True),
        ("response_model_exclude_defaults", True),
    ],
)
def test_contract_changing_serialization_is_rejected(option, value):
    router = StreamRouter()

    async def events() -> AsyncIterator[ContractEvent]:
        yield ContractEvent(**{"external-value": "ok", "secret": "s", "nullable": None})

    with pytest.raises(TypeError, match=option):
        router.stream("/events", **{option: value})(events)
    assert router.routes == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_noop_serialization_preserves_wire_contract_and_generated_model(
    tmp_path, method
):
    router = StreamRouter()

    @router.stream(
        "/events",
        methods=[method],
        operation_id="events",
        response_model_include=None,
        response_model_exclude=set(),
        response_model_by_alias=True,
        response_model_exclude_none=False,
        response_model_exclude_unset=False,
        response_model_exclude_defaults=False,
        status_code=201,
        tags=["contract"],
        responses={201: {"headers": {"x-contract": {"schema": {"type": "string"}}}}},
    )
    async def events() -> AsyncIterator[ContractEvent]:
        yield ContractEvent(**{"external-value": "ok", "secret": "s", "nullable": None})

    application = FastAPI()
    application.include_router(router)
    contract = build_ir(export_schema(application))
    with TestClient(application) as client:
        response = client.request(method, "/events")
    assert response.status_code == 201
    data = json.loads(
        next(
            line[6:] for line in response.text.splitlines() if line.startswith("data: ")
        )
    )
    assert data == {"external-value": "ok", "secret": "s", "nullable": None}
    with load_models(tmp_path, generate_python(contract)) as models:
        parsed = TypeAdapter(models.ContractEvent).validate_python(data)
        assert parsed.model_dump(by_alias=True) == data


async def test_disconnect_closes_generator_and_request_dependencies():
    closed = asyncio.Event()
    dependency_closed = asyncio.Event()
    sent = asyncio.Event()

    async def resource() -> AsyncIterator[str]:
        try:
            yield "connection"
        finally:
            dependency_closed.set()

    router = StreamRouter()

    @router.stream("/events")
    async def events(
        resource: Annotated[str, Depends(resource)],
    ) -> AsyncIterator[Event]:
        try:
            yield Event(value=resource)
            await asyncio.Event().wait()
        finally:
            closed.set()

    application = FastAPI()
    application.include_router(router)
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"data:" in message.get(
            "body", b""
        ):
            sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1000),
        "server": ("localhost", 80),
    }
    await asyncio.wait_for(application(scope, receive, send), timeout=2)
    assert sent.is_set()
    assert closed.is_set()
    assert dependency_closed.is_set()
