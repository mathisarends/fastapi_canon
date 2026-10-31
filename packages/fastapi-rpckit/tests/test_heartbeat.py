import asyncio
import json
from contextlib import asynccontextmanager

from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcLimits,
    RpcMiddleware,
    RpcService,
)
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory


async def test_ping_bypasses_saturated_handlers_and_observation():
    entered = asyncio.Event()
    release = asyncio.Event()
    middleware_calls = []

    class Middleware(RpcMiddleware):
        @asynccontextmanager
        async def request(self, scope):
            middleware_calls.append(scope)
            yield

    channel = RpcChannel("test")

    @channel.method()
    async def work() -> None:
        entered.set()
        await release.wait()

    service = RpcService(
        limits=RpcLimits(max_concurrency=1, max_pending_requests=1),
        middleware=[Middleware()],
    )
    service.socket("/rpc", channels=[channel])
    async with InMemorySocketFactory(service) as factory:
        client = await factory("ws://test/rpc")
        await client.send(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.work"})
        )
        await entered.wait()
        for request in (
            {"jsonrpc": "2.0", "id": 2, "method": "$/ping", "params": {}},
            [{"jsonrpc": "2.0", "id": "ping", "method": "$/ping"}],
        ):
            await client.send(json.dumps(request))
            response = json.loads(await client.recv())
            item = response[0] if isinstance(response, list) else response
            assert item["result"] is None
        assert len(middleware_calls) == 1
        await client.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "method": "$/ping", "params": {"bad": True}}
            )
        )
        assert json.loads(await client.recv())["error"]["code"] == -32600
        assert len(middleware_calls) == 1
        release.set()
        assert json.loads(await client.recv())["id"] == 1
