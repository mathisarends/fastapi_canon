from pydantic import TypeAdapter, ValidationError

from examples.agent_runs.generated_python import (
    Client,
    HttpError,
    StreamParseError,
    TransportError,
)
from examples.agent_runs.generated_python.models import StreamRunEventsErrorBody

_ERROR_BODY = TypeAdapter(StreamRunEventsErrorBody)


async def consume_with_errors(client: Client, run_id: str) -> None:
    try:
        async for event in client.agent_runs.stream_run_events(run_id=run_id):
            if event.type == "run.failed":
                print(event.message)
            else:
                print(event)
    except HttpError as error:
        if error.status == 422:
            try:
                body = _ERROR_BODY.validate_python(error.details)
            except ValidationError:
                pass
            else:
                print("Invalid stream parameters", body.detail or [])
                return
        print("Unexpected HTTP response", error.status, error.details)
        raise
    except StreamParseError as error:
        print("Invalid event data", error.operation_id, error.category)
        raise
    except TransportError as error:
        print("Stream transport failed", str(error))
        raise
