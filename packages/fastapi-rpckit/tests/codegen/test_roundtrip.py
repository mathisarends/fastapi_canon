import asyncio
from collections.abc import AsyncIterator, Callable
from types import ModuleType
from typing import Any

import pytest
from fastapi_canon.integrations.websockets import RpcServer

from tests.conftest import GreetingState, greeting_app


class LoopbackTransport:
    """Carries requests straight into an RpcServer, over JSON like the wire."""

    def __init__(
        self,
        server: RpcServer,
        remote_error: Callable[[int, str, str, Any], Exception],
    ) -> None:
        self._server = server
        self._remote_error = remote_error
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._next_id = 0
        self.closed = False

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        metadata=None,
    ) -> Any:
        self._next_id += 1
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        response = await self._server.handle(request)
        assert response is not None
        payload = response.model_dump(mode="json")
        if "error" in payload:
            rpc_error = payload["error"]
            data = rpc_error["data"]
            raise self._remote_error(
                rpc_error["code"],
                data["code"],
                rpc_error["message"],
                data.get("details"),
            )
        return payload["result"]

    def publish(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self._queue.get()) is not None:
            yield message

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


@pytest.fixture
def handler() -> GreetingState:
    return GreetingState()


@pytest.fixture
def transport(
    handler: GreetingState,
    generated_client: ModuleType,
) -> LoopbackTransport:
    from tests.conftest import TestResolver

    server = greeting_app.create_server(resolver=TestResolver(handler))
    errors = __import__("greeting_client.errors", fromlist=["error_from_response"])
    return LoopbackTransport(server, errors.error_from_response)


async def test_a_generated_call_reaches_the_handler_and_returns_a_model(
    generated_client: ModuleType,
    transport: LoopbackTransport,
    handler: GreetingState,
) -> None:
    client = generated_client.GreetingClient(transport)

    result = await client.greeting.say(name="Mathis")

    assert result.text == "Hello, Mathis!"
    assert type(result).__name__ == "SayResult"
    assert handler.greeted == ["Mathis"]


async def test_a_call_without_a_result_returns_none(
    generated_client: ModuleType,
    transport: LoopbackTransport,
    handler: GreetingState,
) -> None:
    client = generated_client.GreetingClient(transport)
    await client.greeting.say(name="Mathis")

    assert await client.greeting.forget(name="Mathis") is None
    assert handler.greeted == []


async def test_a_server_failure_surfaces_as_a_remote_error(
    generated_client: ModuleType,
    transport: LoopbackTransport,
) -> None:
    client = generated_client.GreetingClient(transport)

    with pytest.raises(generated_client.RpcRemoteError) as error:
        await client.greeting.forget(name="nobody")

    assert error.value.rpc_code == -32001
    assert error.value.code == "unknown_greeting"
    assert error.value.message == "Unknown greeting: nobody"


async def test_notifications_are_parsed_into_typed_payloads(
    generated_client: ModuleType,
    transport: LoopbackTransport,
) -> None:
    client = generated_client.GreetingClient(transport)
    transport.publish(
        {
            "jsonrpc": "2.0",
            "method": "greeting.changed",
            "params": {"type": "greeting.said", "text": "Hello!"},
        }
    )

    notification = await anext(client.greeting.changed())

    assert notification.text == "Hello!"
    assert type(notification).__name__ == "GreetingSaid"


async def test_the_client_closes_its_transport(
    generated_client: ModuleType,
    transport: LoopbackTransport,
) -> None:
    async with generated_client.GreetingClient(transport) as client:
        await client.greeting.say(name="Mathis")

    assert transport.closed


async def test_close_is_idempotent_and_can_leave_a_shared_transport_open(
    generated_client: ModuleType,
    transport: LoopbackTransport,
) -> None:
    client = generated_client.GreetingClient(transport, close_transport=False)

    await client.close()
    await client.close()

    assert not transport.closed


async def test_a_call_without_params_takes_no_arguments(
    generated_client: ModuleType,
    transport: LoopbackTransport,
) -> None:
    client = generated_client.GreetingClient(transport)
    await client.greeting.say(name="Mathis")

    greeted = await client.greeting.greeted()

    assert greeted.names == ["Mathis"]


async def test_a_call_without_params_or_result_returns_none(
    generated_client: ModuleType,
    transport: LoopbackTransport,
    handler: GreetingState,
) -> None:
    client = generated_client.GreetingClient(transport)
    await client.greeting.say(name="Mathis")

    assert await client.greeting.clear() is None
    assert handler.greeted == []
