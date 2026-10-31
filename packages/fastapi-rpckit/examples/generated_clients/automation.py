import asyncio
from collections.abc import AsyncIterator

from fastapi_canon.integrations.websockets import (
    Inject,
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcChannel,
    RpcErrorBinding,
    RpcMessageType,
    RpcModel,
    RpcRejection,
    RpcService,
    RpcSocket,
    RpcSocketProtocol,
    ServerVariable,
)


class Task(RpcModel):
    id: str
    title: str
    done: bool = False


class TaskList(RpcModel):
    tasks: list[Task]


class CreateTaskParams(RpcModel):
    title: str


class DeleteTaskParams(RpcModel):
    task_id: str


class TaskUpdated(RpcModel):
    task: Task


class Tab(RpcModel):
    id: str
    url: str


class OpenTabParams(RpcModel):
    url: str


class StartScreencastParams(RpcModel):
    quality: int = 80


class BrowserNotFoundError(Exception):
    pass


BROWSER_NOT_FOUND = RpcErrorBinding(
    BrowserNotFoundError,
    code="browser_not_found",
    rejection=RpcRejection.NOT_FOUND,
    message="Unknown browser",
)


tasks = RpcChannel("tasks")
# Declared for the contract; clients without a binding get a generic socket.
CDP = RpcSocketProtocol(name="cdp", version="1.3")

browser = RpcChannel("browser")
tabs = browser.child("tabs")
screencast = browser.child("screencast")

_tasks: dict[str, Task] = {}
_updates: asyncio.Queue[Task] = asyncio.Queue()


@tasks.method("list", summary="List all tasks.")
async def list_tasks() -> TaskList:
    return TaskList(tasks=list(_tasks.values()))


@tasks.method(summary="Create a task.")
async def create(params: CreateTaskParams) -> Task:
    task = Task(id=str(len(_tasks) + 1), title=params.title)
    _tasks[task.id] = task
    await _updates.put(task)
    return task


@tasks.method(summary="Delete a task.")
async def delete(params: DeleteTaskParams) -> bool:
    return _tasks.pop(params.task_id, None) is not None


@tasks.event(summary="Stream task updates.")
async def updated() -> AsyncIterator[TaskUpdated]:
    while True:
        yield TaskUpdated(task=await _updates.get())


@tabs.method(summary="Open a browser tab.")
async def open(params: OpenTabParams) -> Tab:
    return Tab(id="tab-1", url=params.url)


@screencast.method(summary="Start the browser screencast.")
async def start(params: StartScreencastParams) -> None:
    return None


@screencast.stream(
    content_type="image/jpeg",
    summary="Raw screencast frames as binary WebSocket messages.",
)
async def frames() -> AsyncIterator[bytes]:
    yield b"\xff\xd8\xff\xd9"


@screencast.stream(
    input_content_type="video/webm",
    summary="Upload a recorded screencast as binary WebSocket messages.",
)
async def upload(recording: Inject[RpcBinaryInput]) -> None:
    async for _chunk in recording:
        pass


@screencast.stream(
    content_type="image/jpeg",
    input_content_type="application/x-input-event",
    summary="Receive frames while sending input events back.",
)
async def control(
    events: Inject[RpcBinaryInput],
    output: Inject[RpcBinaryOutput],
) -> None:
    async for _event in events:
        await output.send(b"\xff\xd8\xff\xd9")


@browser.socket(
    protocol=CDP,
    messages=(RpcMessageType.TEXT,),
    closes=(BROWSER_NOT_FOUND,),
    summary="Proxy the Chrome DevTools Protocol unchanged.",
)
async def cdp(socket: Inject[RpcSocket]) -> None:
    async for message in socket:
        await socket.send(message)


service = RpcService()
service.socket("/automation/rpc", channels=(tasks,), name="production")
service.socket("/browser/rpc", channels=(tabs,), name="browser")
service.socket(
    "/browser/stream",
    channels=(screencast,),
    name="streaming",
    subprotocol="fastapi_canon.integrations.websockets.jsonrpc",
)
service.stream("/browser/screencast", frames)
service.stream("/browser/screencast/upload", upload)
service.stream("/browser/screencast/control", control)
service.socket("/browser/cdp", cdp)

contract = service.contract(
    title="Automation",
    base_url="wss://{host}",
    variables={"host": ServerVariable(default="api.example.com")},
)
