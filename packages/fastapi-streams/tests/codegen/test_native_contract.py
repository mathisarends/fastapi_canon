from copy import deepcopy

import httpx
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.python import generate_python

from examples.native_proxy import create_app
from examples.rest_codegen import rest_codegen_document


def test_native_contract_and_private_rest_view_share_event_models():
    app = create_app()
    authoritative = export_schema(app)
    before = deepcopy(authoritative)
    generated = generate_python(build_ir(authoritative))
    assert "class Started" in generated["models.py"]
    assert "pattern='^run_" in generated["models.py"]
    private = rest_codegen_document(authoritative)
    assert authoritative == before and authoritative["openapi"] == "3.2.0"
    assert private["openapi"] == "3.1.0"
    media = private["paths"]["/runs/{run_id}/events"]["get"]["responses"]["200"][
        "content"
    ]["text/event-stream"]
    assert "itemSchema" not in media
    assert media["schema"]["discriminator"]["propertyName"] == "type"
    assert private["components"] == authoritative["components"]


async def test_native_proxy_preserves_bytes_status_and_closes_upstream():
    app = create_app()
    raw = b': keepalive\r\ndata: {"type":"future"}\r\n\r\n'
    responses = []

    def upstream(request):
        response = httpx.Response(
            202,
            stream=httpx.ByteStream(raw),
            headers={"content-type": "text/event-stream"},
        )
        responses.append(response)
        return response

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(upstream), base_url="https://upstream.test"
        ) as native,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        app.state.upstream = native
        response = await client.get("/runs/run_abc/events")
    assert response.status_code == 202
    assert response.content == raw
    assert responses[0].is_closed
