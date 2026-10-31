import asyncio
import importlib
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from fastapi_canon.integrations.websockets import RpcChannel, RpcService
from fastapi_canon.integrations.websockets.adapters.opentelemetry import (
    OpenTelemetry,
    trace_metadata,
)
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import TypeAdapter

from .conftest import document as document
from .test_connect import client_module as client_module
from .test_reconnect import Transport
from .test_reconnect import pool_runtime as pool_runtime


class InjectMetadata:
    @asynccontextmanager
    async def request(self, call):
        call.metadata.update(trace_metadata())
        yield


async def test_python_middleware_injects_active_trace_into_real_no_params_call(
    client_module,
):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    channel = RpcChannel("trace")

    @channel.method()
    async def ping() -> str:
        return "pong"

    service = RpcService(
        middleware=[OpenTelemetry(tracer_provider=provider, metrics=False)]
    )
    service.socket("/rpc", channels=[channel])
    runtime = importlib.import_module(f"{client_module.__name__}.internal")
    async with (
        InMemorySocketFactory(service) as factory,
        client_module.GreetingClient.connect(
            socket_factory=factory, lazy=True, middleware=[InjectMetadata()]
        ) as client,
    ):
        with provider.get_tracer("test").start_as_current_span("caller") as caller:
            route = runtime.RpcRouteInfo(
                method="trace.ping",
                server="primary",
                result_adapter=TypeAdapter(str),
            )
            assert await client._rpc.request(route) == "pong"
            assert trace.get_current_span() is caller
    span = next(
        item for item in exporter.get_finished_spans() if item.name == "trace.ping"
    )
    assert span.parent.is_remote
    assert span.parent.span_id == caller.get_span_context().span_id
    assert all(task.done() for task in factory._tasks)
    provider.shutdown()


@pytest.mark.parametrize(
    "metadata", [{"other": "x"}, {"baggage": "x" * 4096}, {"traceparent": 42}]
)
async def test_bad_client_metadata_is_observed_and_never_sent_or_retried(
    pool_runtime, metadata
):
    seen, sent = [], []

    class Middleware:
        @asynccontextmanager
        async def request(self, call):
            call.metadata.update(metadata)
            try:
                yield
            finally:
                seen.append(call.outcome.error)

    class Adapter(Transport):
        async def request(self, method, params=None, **options):
            sent.append(method)

    core = pool_runtime.RpcClientCore(
        Adapter(), middleware=[Middleware()], supports_metadata=True, retry_attempts=2
    )
    with pytest.raises(ValueError) as raised:
        await core.request(pool_runtime.RpcRouteInfo("ping", TypeAdapter(str)))
    assert seen == [raised.value] and not sent
    await core.close()


async def test_contract_capability_and_current_transport_typing(
    document, tmp_path, pool_runtime
):
    old = deepcopy(document)
    old.pop("x-rpc-request-metadata")
    generate_python_client(old, tmp_path / "old", PythonClientOptions(package="old"))
    assert "supports_metadata=False" in (tmp_path / "old/client.py").read_text()
    consumer = tmp_path / "adapter_types.py"
    consumer.write_text(
        """
from collections.abc import AsyncIterator, Mapping
from typing import Any
from old import RpcTransport
from old.internal import RpcClientCore

class Adapter:
    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, metadata: Mapping[str, str] | None = None) -> Any:
        return None
    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        yield {}
    async def close(self) -> None:
        pass

transport: RpcTransport = Adapter()
client = RpcClientCore(transport)
""",
        encoding="utf-8",
    )
    checked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.12",
            str(consumer),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr

    class Inject:
        @asynccontextmanager
        async def request(self, call):
            call.metadata["traceparent"] = "invalid"
            yield

    adapter = Transport()
    core = pool_runtime.RpcClientCore(adapter, middleware=[Inject()])
    with pytest.raises(ValueError, match="contract does not support"):
        await core.request(pool_runtime.RpcRouteInfo("ping", TypeAdapter(str)))
    await core.close()
    core = pool_runtime.RpcClientCore(Transport())
    assert (
        await core.request(pool_runtime.RpcRouteInfo("ping", TypeAdapter(str))) == "ok"
    )
    await core.close()


async def test_subscription_controls_each_carry_middleware_metadata(pool_runtime):
    sent = []

    class Inject:
        @asynccontextmanager
        async def request(self, call):
            call.metadata["traceparent"] = "context"
            yield

    class Adapter(Transport):
        async def request(self, method, params=None, *, metadata=None):
            sent.append((method, metadata))
            if method.endswith(".subscribe"):
                return {"subscriptionId": "one"}
            return None

        async def notifications(self):
            yield {
                "jsonrpc": "2.0",
                "method": "updates",
                "params": {"subscriptionId": "one", "payload": "ready"},
            }
            await asyncio.Event().wait()

    core = pool_runtime.RpcClientCore(
        Adapter(), middleware=[Inject()], supports_metadata=True
    )
    notification = pool_runtime.RpcNotificationInfo(
        "updates", TypeAdapter(dict), TypeAdapter(str)
    )
    stream = core.subscribe_remote(notification)
    assert await anext(stream) == "ready"
    await stream.aclose()
    assert sent == [
        ("updates.subscribe", {"traceparent": "context"}),
        ("updates.unsubscribe", {"traceparent": "context"}),
    ]
    await core.close()
