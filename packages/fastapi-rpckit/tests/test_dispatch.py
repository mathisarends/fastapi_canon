import pytest
from fastapi_canon.integrations.websockets import (
    RpcChannel,
    RpcInvalidParamsError,
    RpcMethodNotFoundError,
)
from fastapi_canon.integrations.websockets.dispatch import RpcDispatcher
from fastapi_canon.integrations.websockets.protocol import RpcProtocol
from pydantic import BaseModel

from .conftest import (
    GreetingRpcMethod,
    GreetingState,
    SayParams,
    SayResult,
    TestResolver,
)


def _dispatcher(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> RpcDispatcher:
    return RpcDispatcher(protocol, resolver=TestResolver(handler))


def _request(name: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "method": name, "params": params}


def test_parsing_resolves_the_method_and_validates_the_params(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)

    invocation = dispatcher.parse_request(
        _request(GreetingRpcMethod.SAY, {"name": "Mathis"})
    )

    assert invocation.method.handler_name == "say"
    assert isinstance(invocation.params, SayParams)
    assert type(invocation.params) is SayParams
    assert invocation.params.name == "Mathis"
    assert invocation.request.expects_response


async def test_execution_invokes_the_bound_handler(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)
    invocation = dispatcher.parse_request(
        _request(GreetingRpcMethod.SAY, {"name": "Mathis"})
    )

    result = await dispatcher.execute(invocation)

    assert result == SayResult(text="Hello, Mathis!")
    assert handler.greeted == ["Mathis"]


def test_a_request_without_an_id_is_a_notification(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)

    invocation = dispatcher.parse_request(
        {"jsonrpc": "2.0", "method": GreetingRpcMethod.SAY, "params": {"name": "M"}}
    )

    assert not invocation.request.expects_response


def test_unknown_methods_are_rejected(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)

    with pytest.raises(RpcMethodNotFoundError):
        dispatcher.parse_request(_request("greeting.unknown", {}))


def test_invalid_params_name_the_offending_field(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)

    with pytest.raises(RpcInvalidParamsError) as error:
        dispatcher.parse_request(_request(GreetingRpcMethod.SAY, {}))

    assert "params.name" in error.value.message
    assert error.value.code == "invalid_params"


def test_params_sent_to_a_method_without_params_are_rejected(
    protocol: RpcProtocol,
    handler: GreetingState,
) -> None:
    dispatcher = _dispatcher(protocol, handler)

    with pytest.raises(RpcInvalidParamsError) as error:
        dispatcher.parse_request(_request(GreetingRpcMethod.CLEAR, {"name": "Mathis"}))

    assert "params.name" in error.value.message
    assert error.value.code == "invalid_params"


def test_foreign_model_params_keep_declared_class_and_wire_aliases() -> None:
    class ForeignParams(BaseModel):
        user_id: int

    channel = RpcChannel("foreign")

    @channel.method()
    async def accept(params: ForeignParams) -> None: ...

    invocation = RpcDispatcher(channel.protocol).parse_request(
        _request("foreign.accept", {"userId": 7})
    )
    assert type(invocation.params) is ForeignParams
    assert invocation.params.user_id == 7
