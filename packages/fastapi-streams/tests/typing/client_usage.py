"""Intentional type errors asserted by test_python_typing.py."""

from typing import assert_never

from examples.agent_runs.generated_python import Client, RunEventType


async def valid_usage(client: Client) -> None:
    async for event in client.agent_runs.stream_run_events(run_id="demo"):
        match event.type:
            case RunEventType.RUN_STARTED | RunEventType.RUN_COMPLETED:
                print(event.run_id)
            case RunEventType.TOKEN_DELTA:
                print(event.text)
            case RunEventType.RUN_FAILED:
                print(event.message)
            case _:
                assert_never(event)


async def invalid_usage(client: Client) -> None:
    client.agent_runs.stream_run_events()  # expect-error: reportCallIssue
    client.agent_runs.stream_run_events(run_id=1)  # expect-error: reportArgumentType
    client.agent_runs.stream_prompt(run_id="x")  # expect-error: reportCallIssue
    async for event in client.agent_runs.stream_run_events(run_id="demo"):
        if event.type == RunEventType.TOKEN_DELTA:
            print(event.run_id)  # expect-error: reportAttributeAccessIssue
