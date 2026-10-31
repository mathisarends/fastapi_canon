"""Run with: uv run --extra streams --with uvicorn uvicorn examples.streams.app:app."""

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from dishka import FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from fastapi_canon import Composition, Error, ErrorRegistry, Feature, HTTPMethod
from fastapi_canon.integrations.sse import CanonStreamRouter


class AuthenticationRequired(Exception):
    pass


class RunNotFound(Exception):
    pass


class RunProgress(BaseModel):
    type: Literal["run.progress"] = "run.progress"
    step: int


class RunCompleted(BaseModel):
    type: Literal["run.completed"] = "run.completed"
    run_id: int


class RunRequest(BaseModel):
    steps: int = Field(ge=1, le=3)


type RunEvent = Annotated[RunProgress | RunCompleted, Field(discriminator="type")]


class RunService:
    async def require_existing(self, run_id: int) -> None:
        if run_id <= 0:
            raise RunNotFound

    async def events(self, run_id: int, *, steps: int = 1) -> AsyncIterator[RunEvent]:
        for step in range(1, steps + 1):
            yield RunProgress(step=step)
        yield RunCompleted(run_id=run_id)


class RunProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def run_service(self) -> RunService:
        return RunService()


authentication_required = Error(
    AuthenticationRequired,
    status=401,
    code="authentication_required",
    title="Authentication required",
)
run_not_found = Error(
    RunNotFound,
    status=404,
    code="run_not_found",
    title="Run not found",
)
run_errors = ErrorRegistry(
    name="runs",
    errors=[authentication_required, run_not_found],
    type_base="https://example.test/problems",
)


async def require_authentication(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization == "Bearer forbidden":
        raise HTTPException(status_code=403, detail="Access denied")
    if authorization != "Bearer demo":
        raise AuthenticationRequired


@inject
async def require_existing_run(run_id: int, runs: FromDishka[RunService]) -> None:
    await runs.require_existing(run_id)


router = CanonStreamRouter(
    prefix="/runs",
    tags=["runs"],
    error_registry=run_errors,
    raises=[authentication_required],
    dependencies=[Depends(require_authentication)],
    responses=run_errors.responses(http_statuses=[403]),
)


@router.stream(
    "/{run_id}/events",
    operation_id="run_events",
    raises=[run_not_found],
    dependencies=[Depends(require_existing_run)],
)
@inject
async def events(run_id: int, runs: FromDishka[RunService]) -> AsyncIterator[RunEvent]:
    async for event in runs.events(run_id):
        yield event


@router.stream(
    "/{run_id}/events",
    methods=[HTTPMethod.POST],
    operation_id="create_run_events",
    raises=[run_not_found],
    dependencies=[Depends(require_existing_run)],
)
@inject
async def create_events(
    run_id: int, body: RunRequest, runs: FromDishka[RunService]
) -> AsyncIterator[RunEvent]:
    async for event in runs.events(run_id, steps=body.steps):
        yield event


feature = Feature(name="runs", routers=[router], providers=[RunProvider])
composition = Composition(feature)


def create_app() -> FastAPI:
    return composition.apply(FastAPI())


app = create_app()
