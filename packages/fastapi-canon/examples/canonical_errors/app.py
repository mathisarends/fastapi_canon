from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel

from fastapi_canon import CanonRouter, Composition, Error, ErrorRegistry, Feature
from fastapi_canon.integrations.sse import CanonStreamRouter
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.adapters.fastapi import RpcRoutes


class ProjectMissing(Exception):
    pass


class MissingFields(BaseModel):
    replacement: str | None = None


class ProjectEvent(BaseModel):
    sequence: int


project_missing = Error(
    ProjectMissing,
    status=404,
    code="project_missing",
    title="Project not found",
    detail="Choose another project.",
    extensions_model=MissingFields,
    extensions={"replacement": None},
)
errors = ErrorRegistry(
    errors=[project_missing], type_base="https://api.example.com/problems"
)
http = CanonRouter(error_registry=errors)
streams = CanonStreamRouter(error_registry=errors)
projects = RpcChannel("projects", error_registry=errors)


def require_project(project_id: int) -> None:
    if project_id == 0:
        raise ProjectMissing("private database information")


@http.get("/projects/{project_id}", raises=[project_missing])
async def get_project(project_id: int) -> dict[str, int]:
    require_project(project_id)
    return {"id": project_id}


@streams.stream(
    "/projects/{project_id}/events",
    operation_id="events",
    tags=["projects"],
    raises=[project_missing],
    dependencies=[Depends(require_project)],
    stream_raises=[project_missing],
)
async def events(project_id: int) -> AsyncIterator[ProjectEvent]:
    yield ProjectEvent(sequence=1)
    if project_id == 2:
        raise ProjectMissing("private database information")


class CancelProject(BaseModel):
    project_id: int


@projects.method(raises=[project_missing])
async def cancel(params: CancelProject) -> None:
    require_project(params.project_id)


service = RpcService()
endpoint = service.socket("/ws", channels=[projects])
websockets = APIRouter()
RpcRoutes(websockets).mount(endpoint)
feature = Feature(name="projects", routers=[http, streams, websockets])
contract = service.contract(title="Projects", base_url="wss://api.example.com")


def create_app() -> FastAPI:
    return Composition(feature).apply(FastAPI())


app = create_app()
