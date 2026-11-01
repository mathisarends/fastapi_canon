import importlib.util

import fastapi_canon.integrations.websockets
import pytest
from fastapi_canon.integrations.websockets import RpcRejection, RpcServer


@pytest.mark.parametrize(
    "name",
    (
        "RpcFeatureDefinition",
        "RpcHandler",
        "RpcNotificationDefinition",
        "RpcApp",
        "RpcRouter",
        "OpenRpcContract",
        "OpenRpcServer",
        "ConnectionRejected",
        "feature",
        "event",
        "method",
        "notification",
        "RpcObserver",
        "RpcObserverLike",
        "RpcResponseContext",
        "RpcConnectionContext",
    ),
)
def test_removed_pre_v1_composition_symbols_are_not_public(name: str) -> None:
    assert not hasattr(fastapi_canon.integrations.websockets, name)


def test_servers_are_created_by_an_app() -> None:
    with pytest.raises(TypeError, match="RpcChannel.create_server"):
        RpcServer()


def test_authentication_rejections_are_public() -> None:
    assert RpcRejection.UNAUTHORIZED.value == "unauthorized"
    assert RpcRejection.FORBIDDEN.value == "forbidden"


def test_test_helpers_are_distributed() -> None:
    assert (
        importlib.util.find_spec("fastapi_canon.integrations.websockets.testing")
        is not None
    )


@pytest.mark.parametrize("name", ["fastapi", "dishka", "opentelemetry"])
def test_integrations_have_no_legacy_module_aliases(name: str) -> None:
    assert (
        importlib.util.find_spec(
            f"fastapi_canon.integrations.websockets.adapters.{name}"
        )
        is not None
    )
    assert (
        importlib.util.find_spec(f"fastapi_canon.integrations.websockets.{name}")
        is None
    )


def test_public_signature_types_are_exported() -> None:
    for name in (
        "RpcResolverLike",
        "RpcResponseMessage",
        "RpcProtocol",
        "RpcErrorBinding",
        "RpcErrorDeclaration",
    ):
        assert name in fastapi_canon.integrations.websockets.__all__


def test_observer_module_is_removed():
    assert (
        importlib.util.find_spec("fastapi_canon.integrations.websockets.observer")
        is None
    )


@pytest.mark.parametrize("factory", ["service", "channel", "endpoint"])
def test_observer_registration_is_removed(factory):
    from fastapi_canon.integrations.websockets import RpcChannel, RpcService

    channel = RpcChannel("test")
    with pytest.raises(TypeError, match="observer"):
        if factory == "service":
            RpcService(observer=object())
        elif factory == "channel":
            channel.create_server(observer=object())
        else:
            RpcService().socket("/rpc", channels=[channel], observer=object())
