from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field, TypeAdapter
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse


class Started(BaseModel):
    type: Literal["started"]
    run_id: str = Field(pattern="^run_[A-Za-z0-9_-]+$")


class Done(BaseModel):
    type: Literal["done"]
    run_id: str = Field(pattern="^run_[A-Za-z0-9_-]+$")


type Event = Annotated[Started | Done, Field(discriminator="type")]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with httpx.AsyncClient(
        base_url="https://upstream.example.test", timeout=None
    ) as client:
        app.state.upstream = client
        yield


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    payload = TypeAdapter(Event).json_schema(
        ref_template="#/components/schemas/{model}"
    )
    definitions = payload.pop("$defs", {})

    @app.get(
        "/runs/{run_id}/events",
        operation_id="run_events",
        tags=["runs"],
        response_class=StreamingResponse,
        openapi_extra={
            "responses": {
                "200": {
                    "content": {
                        "text/event-stream": {
                            "itemSchema": {
                                "type": "object",
                                "properties": {
                                    "data": {
                                        "type": "string",
                                        "contentMediaType": "application/json",
                                        "contentSchema": payload,
                                    }
                                },
                                "required": ["data"],
                            },
                        }
                    }
                }
            }
        },
    )
    async def events(run_id: str, request: Request) -> StreamingResponse:
        upstream = request.app.state.upstream
        # The application's boundary owns authentication and allowed upstream headers.
        response = await upstream.send(
            upstream.build_request("GET", f"/runs/{run_id}/events"), stream=True
        )
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in {"content-type", "content-encoding", "cache-control"}
        }
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(response.aclose),
        )

    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        document = original_openapi()
        document.setdefault("components", {}).setdefault("schemas", {}).update(
            definitions
        )
        return document

    app.openapi = openapi
    return app


app = create_app()
