from typing import Annotated, Literal

import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import BaseModel, Field, ValidationError

from tests.codegen.helpers import event_contract, load_package


class Started(BaseModel):
    type: Literal["started"]
    run_id: str = Field(pattern="^run_[A-Za-z0-9_-]+$")


class Done(BaseModel):
    type: Literal["done"]


type Event = Annotated[Started | Done, Field(discriminator="type")]


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("policy", ["error", "ignore"])
@pytest.mark.parametrize(
    ("payload", "category", "error_type"),
    [
        ('{"type":', "invalid_json", "json_invalid"),
        (
            '{"type":"started","run_id":"sensitive-invalid"}',
            "invalid_event_schema",
            "string_pattern_mismatch",
        ),
        ('{"type":"started"}', "invalid_event_schema", "missing"),
        ("{}", "invalid_event_schema", "union_tag_not_found"),
        ('{"type":7}', "invalid_event_schema", "union_tag_invalid"),
    ],
)
async def test_structured_errors_retain_cause_without_public_payload_values(
    tmp_path,
    backend,
    policy,
    payload,
    category,
    error_type,
    caplog,
):
    http = __import__(backend)
    with load_package(
        tmp_path / "sdk", generate_python(event_contract(Event), http_client=backend)
    ) as sdk:
        async with (
            http.AsyncClient(
                transport=http.MockTransport(
                    lambda _: http.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        content=f"data: {payload}\n\n",
                    )
                )
            ) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            with pytest.raises(sdk.StreamParseError) as caught:
                await anext(
                    api.streams.events(
                        options=sdk.RequestOptions(unknown_events=policy)
                    )
                )
            error = caught.value
            assert error.operation_id == "events"
            assert error.category == category
            assert any(item["type"] == error_type for item in error.validation_errors)
            assert all(
                "input" not in item and "ctx" not in item
                for item in error.validation_errors
            )
            assert error.data == payload
            assert isinstance(error.__cause__, ValidationError)
            assert payload not in str(error)
            assert "sensitive-invalid" not in caplog.text
            if error_type == "string_pattern_mismatch":
                assert error.validation_errors[0]["loc"] == ("started", "run_id")
