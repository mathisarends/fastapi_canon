from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query
from fastapi_canon.integrations.sse import StreamRouter
from pydantic import BaseModel, Field


class Metadata(BaseModel):
    labels: list[str]
    limits: dict[str, int]
    note: str | None = None
    mode: Literal["fast", "full"] = "fast"


class NestedEvent(BaseModel):
    type: Literal["nested"]
    external_id: Annotated[str, Field(alias="external-id")]
    metadata: Metadata
    children: list[NestedEvent] = Field(default_factory=list)


class DoneEvent(BaseModel):
    type: Literal["done"]
    count: int


type FixtureEvent = Annotated[NestedEvent | DoneEvent, Field(discriminator="type")]

router = StreamRouter(tags=["fixtures"])


@router.stream("/nested/{item_id}", operation_id="nested_events")
async def nested_events(
    item_id: int,
    labels: Annotated[list[str] | None, Query()] = None,
    x_trace: Annotated[str | None, Header()] = None,
) -> AsyncIterator[FixtureEvent]:
    yield NestedEvent(
        type="nested",
        **{
            "external-id": str(item_id),
            "metadata": Metadata(labels=labels or [], limits={"max": 3}, note=x_trace),
        },
    )
    yield DoneEvent(type="done", count=1)


app = FastAPI()
app.include_router(router)
