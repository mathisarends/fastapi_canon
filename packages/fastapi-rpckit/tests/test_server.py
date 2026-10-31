import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcError,
    RpcErrorCode,
    RpcFailure,
    RpcLimits,
    RpcMiddleware,
    RpcServer,
    RpcService,
    RpcSuccess,
)
from pydantic import BaseModel

from .conftest import (
    GreetingRpcMethod,
    GreetingState,
    SayParams,
    TestResolver,
    greeting_app,
)
from .deadlines import ControlledDeadlines


def _server(handler: GreetingState) -> RpcServer:
    return greeting_app.create_server(resolver=TestResolver(handler))


@pytest.mark.parametrize("encoded", [False, True])
async def test_deadlines_include_semaphore_wait_and_method_none_disables_default(
    monkeypatch, encoded
):
    clock = ControlledDeadlines(monkeypatch)
    channel = RpcChannel("deadlines")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    queued_ran = False

    @channel.method()
    async def inherited() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    @channel.method(timeout=3)
    async def queued() -> None:
        nonlocal queued_ran
        queued_ran = True

    @channel.method(timeout=None)
    async def unbounded() -> str:
        await release.wait()
        return "ok"

    server = channel.create_server(
        limits=RpcLimits(max_concurrency=1, request_timeout=10), strict_errors=True
    )

    async def call(method, request_id):
        raw = {"jsonrpc": "2.0", "method": f"deadlines.{method}", "id": request_id}
        if encoded:
            return json.loads(await server.handle_json(json.dumps(raw)))
        return (await server.handle(raw)).model_dump(mode="json")

    running = asyncio.create_task(call("inherited", 1))
    deadline, first = await clock.next()
    assert 9 < deadline - asyncio.get_running_loop().time() <= 10
    await entered.wait()
    waiting = asyncio.create_task(call("queued", 2))
    deadline, second = await clock.next()
    assert 2 < deadline - asyncio.get_running_loop().time() <= 3
    no_deadline = asyncio.create_task(call("unbounded", 3))
    try:
        clock.expire(second)
        response = await asyncio.wait_for(waiting, 1)
        assert response["error"]["data"]["code"] == "request_timeout"
        assert response["error"]["code"] == RpcErrorCode.SERVER_ERROR
        assert not queued_ran
        clock.expire(first)
        assert (await running)["error"]["data"]["code"] == "request_timeout"
        assert cancelled.is_set()
        assert clock.created.empty()
        assert not no_deadline.done()
        release.set()
        assert (await no_deadline)["result"] == "ok"
    finally:
        for task in (running, waiting, no_deadline):
            task.cancel()
        await asyncio.gather(running, waiting, no_deadline, return_exceptions=True)


async def test_deadline_cancels_dependency_resolution_and_exits_its_scope(monkeypatch):

    from fastapi_canon.integrations.websockets import Inject, RpcRequestTimeoutError

    clock = ControlledDeadlines(monkeypatch)
    resolving = asyncio.Event()
    exited = asyncio.Event()

    class Resolver:
        @asynccontextmanager
        async def enter_scope(self):
            try:
                yield self
            finally:
                exited.set()

        async def resolve(self, dependency):
            resolving.set()
            await asyncio.Future()

    channel = RpcChannel("dependencies")

    @channel.method(timeout=2)
    async def work(value: Inject[str]) -> str:
        pytest.fail("Dependency resolution must expire before the handler starts")

    server = channel.create_server(resolver=Resolver())
    task = asyncio.create_task(
        server.handle({"jsonrpc": "2.0", "method": "dependencies.work", "id": 1})
    )
    _, waiting = await clock.next()
    await resolving.wait()
    clock.expire(waiting)
    response = await asyncio.wait_for(task, 1)
    assert response.error.data.code == RpcRequestTimeoutError.code
    assert exited.is_set()


async def test_cancelling_standalone_batch_preserves_outer_cancellation():
    channel = RpcChannel("shutdown")
    started = asyncio.Queue()
    finished = []

    @channel.method()
    async def work() -> None:
        started.put_nowait(True)
        try:
            await asyncio.Future()
        finally:
            finished.append(True)

    server = channel.create_server()
    task = asyncio.create_task(
        server.handle(
            [
                {"jsonrpc": "2.0", "method": "shutdown.work", "id": request_id}
                for request_id in (1, 2)
            ]
        )
    )
    await started.get()
    await started.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True, True]


async def test_handle_json_maps_deep_nesting_to_parse_error(handler, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fastapi_canon.integrations.websockets")
    response = json.loads(await _server(handler).handle_json("[" * 100_000))
    assert response["id"] is None
    assert response["error"]["code"] == -32700
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_deep_any_params_and_recursive_models_have_controlled_outcomes(caplog):
    caplog.set_level(logging.DEBUG, logger="fastapi_canon.integrations.websockets")

    class AnyParams(BaseModel):
        data: dict[str, Any]

    class Tree(BaseModel):
        child: "Tree | None" = None

    channel = RpcChannel("deep")

    @channel.method
    async def arbitrary(params: AnyParams) -> None: ...

    @channel.method
    async def recursive(params: Tree) -> None: ...

    nested = {}
    for _ in range(1000):
        nested = {"child": nested}
    server = channel.create_server()
    accepted = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "deep.arbitrary",
            "params": {"data": nested},
        }
    )
    rejected = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "deep.recursive", "params": nested}
    )
    assert isinstance(accepted, RpcSuccess)
    assert rejected.error.code == RpcErrorCode.INVALID_PARAMS
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("params", [[], ["M"], [1, 2]])
async def test_positional_params_are_invalid_params(handler, params) -> None:
    response = await _server(handler).handle(
        {"jsonrpc": "2.0", "id": 7, "method": GreetingRpcMethod.SAY, "params": params}
    )
    assert response.error.code == RpcErrorCode.INVALID_PARAMS
    assert response.id == 7


async def test_a_request_is_answered_with_its_result(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": GreetingRpcMethod.SAY,
            "params": {"name": "M"},
        }
    )

    assert isinstance(response, RpcSuccess)
    assert response.id == 7
    assert response.result is not None
    assert response.result.text == "Hello, M!"


async def test_middleware_receives_request_outcome_and_duration(
    handler: GreetingState,
) -> None:
    class Instrumentation(RpcMiddleware):
        def __init__(self) -> None:
            self.requests = []
            self.responses = []

        @asynccontextmanager
        async def request(self, scope):
            self.requests.append(scope.request)
            try:
                yield
            finally:
                self.responses.append(scope)

    instrumentation = Instrumentation()

    server = greeting_app.create_server(
        resolver=TestResolver(handler),
        middleware=[instrumentation],
    )
    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": GreetingRpcMethod.SAY,
            "params": {"name": "M"},
        }
    )

    assert instrumentation.requests[0].method == GreetingRpcMethod.SAY
    assert instrumentation.requests[0].request_id == 7
    assert instrumentation.requests[0].notification is False
    assert instrumentation.responses[0].request is instrumentation.requests[0]
    assert instrumentation.responses[0].outcome.response is response
    assert instrumentation.responses[0].outcome.duration >= 0


async def test_a_notification_is_served_without_a_response(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle(
        {"jsonrpc": "2.0", "method": GreetingRpcMethod.SAY, "params": {"name": "M"}}
    )

    assert response is None
    assert handler.greeted == ["M"]


async def test_an_unknown_method_becomes_a_failure(handler: GreetingState) -> None:
    response = await _server(handler).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "greeting.unknown", "params": {}}
    )

    assert isinstance(response, RpcFailure)
    assert response.id == 1
    assert response.error.code == RpcErrorCode.METHOD_NOT_FOUND


async def test_invalid_params_become_a_failure(handler: GreetingState) -> None:
    response = await _server(handler).handle(
        {"jsonrpc": "2.0", "id": 1, "method": GreetingRpcMethod.SAY, "params": {}}
    )

    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INVALID_PARAMS
    assert "params.name" in response.error.message


async def test_a_malformed_envelope_becomes_an_invalid_request(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle({"id": 1, "method": "x"})

    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INVALID_REQUEST


async def test_a_declared_error_goes_on_the_wire_as_declared(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": GreetingRpcMethod.FORGET,
            "params": {"name": "M"},
        }
    )

    assert isinstance(response, RpcFailure)
    assert response.error.code == -32001
    assert response.error.message == "Unknown greeting: M"


async def test_foreign_errors_are_translated_by_the_error_mapper() -> None:
    server = broken_app.create_server(error_mapper=_broken_error)

    response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "greeting.break"}
    )

    assert isinstance(response, RpcFailure)
    assert response.error.code == -32004
    assert response.error.message == "Mapped: boom"


async def test_unmapped_handler_failures_stay_internal(caplog) -> None:
    server = broken_app.create_server()
    with caplog.at_level(logging.ERROR, logger="fastapi_canon.integrations.websockets"):
        response = await server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "greeting.break"}
        )

    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INTERNAL_ERROR
    assert response.error.message == "Internal error"
    assert "RPC method greeting.break failed" in caplog.text
    assert "BreakageError: boom" in caplog.text


async def test_a_non_object_payload_fails_without_an_id(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle("nonsense")

    assert isinstance(response, RpcFailure)
    assert response.id is None


def test_rpc_errors_carry_their_own_code(handler: GreetingState) -> None:
    class BusyError(RpcError):
        rpc_code = -32001

    failure = _server(handler).failure(3, BusyError(message="Busy"))

    assert failure.id == 3
    assert failure.error.code == -32001


async def test_a_boolean_id_on_a_failed_request_is_not_echoed_back(
    handler: GreetingState,
) -> None:
    response = await _server(handler).handle(
        {"jsonrpc": "2.0", "id": True, "method": "greeting.unknown", "params": {}}
    )

    assert isinstance(response, RpcFailure)
    assert response.id is None


@pytest.mark.parametrize("request_id", [True, False, 1.0])
@pytest.mark.parametrize("encoded", [False, True])
async def test_invalid_id_types_cannot_execute_handlers(request_id, encoded):
    channel = RpcChannel("strict")

    @channel.method()
    async def work() -> None:
        pytest.fail("Invalid request IDs must be rejected before execution")

    server = channel.create_server()
    message = {"jsonrpc": "2.0", "method": "strict.work", "id": request_id}
    if encoded:
        response = json.loads(await server.handle_json(json.dumps(message)))
    else:
        response = (await server.handle(message)).model_dump()
    assert response["id"] is None
    assert response["error"]["code"] == -32600


async def test_a_validation_error_in_handler_is_internal(caplog) -> None:
    class NestedParams(BaseModel):
        params: str

    router = RpcChannel("greeting")

    @router.method("broken")
    async def broken(params: SayParams) -> None:
        NestedParams.model_validate({"params": 1})

    with caplog.at_level(logging.ERROR, logger="fastapi_canon.integrations.websockets"):
        response = await router.create_server().handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "greeting.broken",
                "params": {"name": "M"},
            }
        )

    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INTERNAL_ERROR
    assert "ValidationError" in caplog.text


async def test_invalid_handler_result_is_internal(caplog) -> None:
    channel = RpcChannel("result")

    @channel.method()
    async def broken() -> int:
        return "abc"  # type: ignore[return-value]

    with caplog.at_level(logging.ERROR, logger="fastapi_canon.integrations.websockets"):
        response = await channel.create_server().handle(
            {"jsonrpc": "2.0", "id": 1, "method": "result.broken"}
        )

    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INTERNAL_ERROR
    assert "ValidationError" in caplog.text


async def test_matching_dict_handler_result_is_accepted() -> None:
    class Result(BaseModel):
        value: int

    channel = RpcChannel("result")

    @channel.method()
    async def valid() -> Result:
        return {"value": 3}  # type: ignore[return-value]

    response = await channel.create_server().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "result.valid"}
    )

    assert isinstance(response, RpcSuccess)
    assert response.result.value == 3


async def test_batch_runs_concurrently_with_shared_limit() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    running = 0
    peak = 0
    channel = RpcChannel("batch")

    @channel.method()
    async def wait() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if peak == 2:
            started.set()
        await release.wait()
        running -= 1

    server = channel.create_server(limits=RpcLimits(max_concurrency=2))
    batch = [
        {"jsonrpc": "2.0", "id": index, "method": "batch.wait"} for index in range(3)
    ]
    task = asyncio.create_task(server.handle(batch))
    await asyncio.wait_for(started.wait(), 1)
    assert peak == 2
    release.set()
    responses = await asyncio.wait_for(task, 1)
    assert [item.id for item in responses] == [0, 1, 2]


async def test_batch_size_limit_rejects_batch() -> None:
    server = broken_app.create_server(limits=RpcLimits(max_batch_size=0))
    response = await server.handle(
        [{"jsonrpc": "2.0", "id": 1, "method": "greeting.break"}]
    )
    assert isinstance(response, RpcFailure)
    assert response.error.code == RpcErrorCode.INVALID_REQUEST


async def test_custom_error_mapper_and_strict_errors() -> None:
    class DomainMissing(Exception):
        pass

    class MissingError(RpcError):
        pass

    channel = RpcChannel("mapped")

    @channel.method(raises=(MissingError,))
    async def declared() -> None:
        raise DomainMissing("gone")

    @channel.method()
    async def undeclared() -> None:
        raise DomainMissing("gone")

    def mapper(error: Exception) -> RpcError | None:
        return (
            MissingError(message=str(error))
            if isinstance(error, DomainMissing)
            else None
        )

    service = RpcService(error_mapper=mapper, strict_errors=True)
    endpoint = service.socket("/mapped", channels=(channel,))
    server = endpoint.create_server()
    declared_response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "mapped.declared"}
    )
    undeclared_response = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "mapped.undeclared"}
    )
    assert isinstance(declared_response, RpcFailure)
    assert declared_response.error.data.code == "missing"
    assert declared_response.error.message == "gone"
    assert isinstance(undeclared_response, RpcFailure)
    assert undeclared_response.error.code == RpcErrorCode.INTERNAL_ERROR


class BreakageError(Exception):
    pass


class BrokenParams(BaseModel):
    pass


broken_channel = RpcChannel("greeting")


@broken_channel.method("break")
async def fail(params: BrokenParams) -> None:
    raise BreakageError("boom")


broken_app = broken_channel


class MappedError(RpcError):
    rpc_code = -32004


def _broken_error(error: Exception) -> RpcError | None:
    if isinstance(error, BreakageError):
        return MappedError(message=f"Mapped: {error}")
    return None
