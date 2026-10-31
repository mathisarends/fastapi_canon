import time
from types import SimpleNamespace

import fastapi_canon.integrations.websockets.connection as connection_module
import pytest
from fastapi.testclient import TestClient
from fastapi_canon.integrations.websockets import RpcService
from fastapi_canon.integrations.websockets.adapters.dishka import dishka_router
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from examples.authenticated import Login, Sessions, create_app


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 403), ({"origin": "https://app.example.test"}, 401)],
)
def test_authentication_rejects_before_accept(headers, status):
    with TestClient(create_app()) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            client.websocket_connect("/rpc", headers=headers),
        ):
            pass
        assert denied.value.status_code == status


def test_authenticated_calls_and_expiry(monkeypatch):
    timers = []

    def schedule(timestamp, callback):
        timers.append((timestamp, callback))
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(connection_module, "_schedule_expiry", schedule)
    app = create_app()
    with TestClient(app) as client:
        sessions = client.portal.call(app.state.dishka_container.get, Sessions)
        login = Login("grace", time.time() + 3600)
        sessions.values["short"] = login
        with client.websocket_connect(
            "/rpc",
            headers={"origin": "https://app.example.test", "cookie": "session=short"},
        ) as socket:
            socket.send_json({"jsonrpc": "2.0", "id": 1, "method": "account.whoami"})
            assert socket.receive_json()["result"] == "grace"
            assert timers[-1][0] == login.expires_at
            client.portal.call(timers[-1][1])
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008
            assert closed.value.reason == "credentials_expired"


def test_dishka_router_reports_context_factory_misuse():
    async def context():
        return object()

    with pytest.raises(TypeError, match="RpcRoutes"):
        dishka_router(RpcService(), context=context)
