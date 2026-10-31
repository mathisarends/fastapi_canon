import runpy
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def test_http_sse_and_rpc_share_application_state_and_close_scopes() -> None:
    example = runpy.run_path(str(ROOT / "examples/stack_service.py"))
    app = example["create_app"]()
    with TestClient(app) as client:
        assert client.post("/counter").json() == {"value": 1}
        response = client.get("/counter/events")
        assert response.status_code == 200
        assert '"value":1' in response.text
        with client.websocket_connect("/rpc") as websocket:
            for request_id, value in ((1, 2), (2, 3)):
                websocket.send_json(
                    {"jsonrpc": "2.0", "id": request_id, "method": "counter.increment"}
                )
                assert websocket.receive_json() == {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"value": value},
                }
        assert '"value":3' in client.get("/counter/events").text
        # Inspect the shared instance through its owning application container.
        counter = client.portal.call(app.state.dishka_container.get, example["Counter"])
        assert counter.calls_closed == 3
        assert not counter.closed
        schema = app.openapi()
        assert schema["paths"]["/counter"]["post"]["operationId"] == "increment_counter"
        assert (
            "text/event-stream"
            in schema["paths"]["/counter/events"]["get"]["responses"]["200"]["content"]
        )
        contract = app.state.rpc_service.contract(
            title="Counter", base_url="ws://testserver"
        ).to_openrpc()
        assert any(
            method["name"] == "counter.increment" for method in contract["methods"]
        )
    assert counter.closed
