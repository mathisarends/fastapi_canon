import importlib

import pytest
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import ConfigDict, TypeAdapter, ValidationError, create_model

from tests.codegen.helpers import (
    event_document,
    load_models,
    load_package,
    schema_contract,
)


@pytest.mark.parametrize("policy", ["omitted", False, True, {"type": "integer"}])
def test_additional_properties_policies_remain_distinct(tmp_path, policy):
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    if policy != "omitted":
        schema["additionalProperties"] = policy
    with load_models(tmp_path, generate_python(schema_contract(schema))) as models:
        adapter = TypeAdapter(models.EventsEvent)
        payload = {"name": "ok", "extra": 123}
        if policy is False:
            with pytest.raises(ValidationError):
                adapter.validate_python(payload)
        else:
            parsed = adapter.validate_python(payload)
            assert parsed.model_dump() == (
                {"name": "ok"} if policy == "omitted" else payload
            )
            if isinstance(policy, dict):
                with pytest.raises(ValidationError):
                    adapter.validate_python({"name": "ok", "extra": "bad"})


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_source_forbid_policy_reaches_request_validation_and_stream_errors(
    tmp_path, backend
):
    source = create_model(
        "Event", __config__=ConfigDict(extra="forbid"), name=(str, ...)
    )
    document = event_document(source)
    operation = document["paths"]["/events"].pop("get")
    document["paths"]["/events"]["post"] = operation
    operation["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Event"}}
        },
    }
    http = importlib.import_module(backend)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.content == b'{"name":"ok"}'
        return http.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content='data: {"name":"ok","extra":1}\n\n',
        )

    with load_package(
        tmp_path, generate_python(build_ir(document), http_client=backend)
    ) as sdk:
        with pytest.raises(ValidationError):
            sdk.models.Event.model_validate({"name": "ok", "extra": 1})
        async with (
            http.AsyncClient(transport=http.MockTransport(handler)) as native,
            sdk.Client("http://example.test", http_client=native) as client,
        ):
            with pytest.raises(ValidationError):
                client.streams.events(body={"name": "ok", "extra": 1})
            assert requests == []
            with pytest.raises(sdk.StreamParseError):
                _ = [
                    event
                    async for event in client.streams.events(
                        body=sdk.models.Event(name="ok")
                    )
                ]
            assert len(requests) == 1
