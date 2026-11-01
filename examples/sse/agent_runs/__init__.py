import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_canon.integrations.sse import StreamRouter
from pydantic import BaseModel, Field


class RunStarted(BaseModel):
    type: Literal["run.started"]
    run_id: str


class TokenDelta(BaseModel):
    type: Literal["token.delta"]
    text: str


class RunCompleted(BaseModel):
    type: Literal["run.completed"]
    run_id: str


class RunFailed(BaseModel):
    type: Literal["run.failed"]
    message: str


type RunEvent = Annotated[
    RunStarted | TokenDelta | RunCompleted | RunFailed, Field(discriminator="type")
]


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    if credentials.credentials != "demo-token":
        raise HTTPException(status_code=403, detail="Use Bearer demo-token")


router = StreamRouter(prefix="/runs", tags=["agentRuns"])


@router.stream("/{run_id}/events", operation_id="stream_run_events")
async def stream_run_events(
    run_id: str,
    delay: Annotated[float, Query(ge=0, le=1)] = 0.01,
) -> AsyncIterator[RunEvent]:
    yield RunStarted(type="run.started", run_id=run_id)
    for text in ["Hello", " ", "世界", " 👋"]:
        await asyncio.sleep(delay)
        yield TokenDelta(type="token.delta", text=text)
    yield RunCompleted(type="run.completed", run_id=run_id)


@router.stream(
    "/{run_id}/events",
    methods=["POST"],
    operation_id="stream_prompt",
    dependencies=[Depends(authorize)],
)
async def stream_prompt(run_id: str, body: RunRequest) -> AsyncIterator[RunEvent]:
    yield RunStarted(type="run.started", run_id=run_id)
    for word in body.prompt.split():
        await asyncio.sleep(0.01)
        yield TokenDelta(type="token.delta", text=word)
    yield RunCompleted(type="run.completed", run_id=run_id)


app = FastAPI(title="Agent runs", version="0.0.1")
app.include_router(router, prefix="/v1")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
