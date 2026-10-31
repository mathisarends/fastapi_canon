import asyncio

import pytest
from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcConnectionExpiry,
    RpcReject,
    RpcRejection,
    RpcService,
)
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory
from pydantic import BaseModel

from tests.test_expiry import expiry_clock as expiry_clock

from .conftest import document as document
from .test_connect import client_module as client_module


@pytest.mark.parametrize("reject_refresh", [False, True])
async def test_expiry_refreshes_python_headers_or_stops_on_denial(
    client_module, expiry_clock, reject_refresh
):
    headers_seen, states = [], asyncio.Queue()
    channel = RpcChannel("greeting")

    class Params(BaseModel):
        name: str

    @channel.method()
    async def say(params: Params) -> dict[str, str]:
        return {"text": params.name}

    async def authenticate(handshake):
        headers_seen.append(handshake.headers["Authorization"])
        if len(headers_seen) == 1:
            return {RpcConnectionExpiry: RpcConnectionExpiry(1000)}
        if reject_refresh:
            raise RpcReject(RpcRejection.UNAUTHORIZED, "Unauthorized")
        return None

    service = RpcService()
    service.socket("/rpc", channels=[channel], before_accept=authenticate)
    calls = 0

    async def headers():
        nonlocal calls
        calls += 1
        return {"Authorization": f"Bearer credential-{calls}"}

    async with (
        InMemorySocketFactory(service) as factory,
        client_module.GreetingClient.connect(
            socket_factory=factory,
            headers=headers,
            lazy=True,
            reconnect=True,
            reconnect_max_attempts=3,
            on_state_change=lambda *state: states.put_nowait(state),
        ) as client,
    ):
        assert (await client.greeting.say(name="before")).text == "before"
        timer = await expiry_clock.get()
        timer.callback()
        if reject_refresh:
            error = await asyncio.wait_for(client.closed, 1)
            assert isinstance(error, client_module.RpcConnectError)
            assert error.permanent
        else:
            while (await asyncio.wait_for(states.get(), 1))[1] != "reconnecting":
                pass
            while (await asyncio.wait_for(states.get(), 1))[1] != "connected":
                pass
            assert (await client.greeting.say(name="after")).text == "after"
        assert calls == 2
        assert headers_seen == ["Bearer credential-1", "Bearer credential-2"]
        assert factory.sockets[0].client.close_reason == "credentials_expired"
