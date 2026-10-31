import asyncio
from typing import assert_never

from examples.agent_runs.generated_python import Client, RunEventType


async def main() -> None:
    async with Client(
        "http://127.0.0.1:8000", headers={"Authorization": "Bearer demo-token"}
    ) as client:
        async for event in client.agent_runs.stream_run_events(run_id="demo"):
            match event.type:
                case RunEventType.RUN_STARTED:
                    print(f"Started {event.run_id}")
                case RunEventType.TOKEN_DELTA:
                    print(event.text, end="", flush=True)
                case RunEventType.RUN_COMPLETED:
                    print(f"\nCompleted {event.run_id}")
                case RunEventType.RUN_FAILED:
                    print(event.message)
                case _:
                    assert_never(event)


if __name__ == "__main__":
    asyncio.run(main())
