from collections.abc import AsyncIterator
from typing import Literal

from fastapi_canon.integrations.websockets import (
    Inject,
    RpcChannel,
    RpcModel,
    RpcService,
)


class JobStarted(RpcModel):
    type: Literal["job.started"] = "job.started"
    job_id: str


class JobFinished(RpcModel):
    type: Literal["job.finished"] = "job.finished"
    job_id: str
    succeeded: bool


type JobUpdate = JobStarted | JobFinished

router = RpcChannel("jobs")


class JobEvents:
    async def subscribe(self) -> AsyncIterator[JobUpdate]:
        yield JobStarted(job_id="job-42")


@router.event(
    "changed",
    summary="Publish job lifecycle changes.",
)
async def job_changes(events: Inject[JobEvents]) -> AsyncIterator[JobUpdate]:
    async for event in events.subscribe():
        yield event


app = RpcService()
app.socket("/rpc", channels=(router,))


def main() -> None:
    notification = app.freeze().notifications[0]
    print(f"Notification source: {notification.name} -> {notification.payload}")
    names = [item.name for item in app.freeze().notification_types]
    print(f"Known notification types: {names}")


if __name__ == "__main__":
    main()
