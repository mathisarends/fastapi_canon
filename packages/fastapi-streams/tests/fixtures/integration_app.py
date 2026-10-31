import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi_canon.integrations.sse import StreamRouter
from pydantic import BaseModel

from examples.agent_runs import router as runs_router
from tests.fixtures.models_app import router as models_router

lifecycles: dict[str, list[str]] = {}


class Tick(BaseModel):
    n: int


async def resource(stream_id: str) -> AsyncIterator[None]:
    lifecycles[stream_id] = ["dependency_open"]
    try:
        yield None
    finally:
        lifecycles[stream_id].append("dependency_closed")


router = StreamRouter(tags=["compat"])


@router.stream("/open/{stream_id}", operation_id="open_stream")
async def open_stream(
    stream_id: str, connection: Annotated[None, Depends(resource)]
) -> AsyncIterator[Tick]:
    try:
        yield Tick(n=1)
        await asyncio.Event().wait()
    finally:
        lifecycles[stream_id].append("generator_closed")


@router.stream("/created", status_code=201, operation_id="created")
async def created() -> AsyncIterator[Tick]:
    yield Tick(n=2)


@router.stream("/nullable", operation_id="nullable_parameters")
async def nullable_parameters(
    limit: int | None = None,
    x_limit: Annotated[int | None, Header()] = None,
) -> AsyncIterator[Tick]:
    yield Tick(n=(limit or 0) + (x_limit or 0))


@router.stream("/required-nullable", operation_id="required_nullable_parameters")
async def required_nullable_parameters(
    limit: Annotated[int | None, Query()],
    x_limit: Annotated[int | None, Header()],
) -> AsyncIterator[Tick]:
    yield Tick(n=(limit or 0) + (x_limit or 0))


parent = APIRouter(prefix="/compat")
parent.include_router(router)
app = FastAPI()
app.include_router(parent)
app.include_router(runs_router, prefix="/v1")
app.include_router(models_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/lifecycle/{stream_id}")
async def lifecycle(stream_id: str) -> list[str]:
    return lifecycles.get(stream_id, [])
