import json
from typing import Annotated, Literal

import httpx
import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import BaseModel, Field, create_model

from tests.codegen.helpers import event_contract, load_package


class Started(BaseModel):
    type: Literal["started"]
    run_id: str


class Done(BaseModel):
    type: Literal["done"]
    count: int


type Event = Annotated[Started | Done, Field(discriminator="type")]


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    ("policy", "payloads", "expected"),
    [
        ("error", [{"type": "future"}], "error"),
        ("ignore", [{"type": "future"}, {"type": "done", "count": 1}], "done"),
        ("ignore", [{"type": "started"}], "error"),
        ("ignore", [{"count": 1}], "error"),
        ("ignore", [{"type": 5}], "error"),
        ("ignore", ["broken-json"], "error"),
    ],
)
async def test_unknown_policy_preserves_known_validation(
    tmp_path, backend, policy, payloads, expected
):
    if backend == "httpx2":
        import httpx2 as http
    else:
        http = httpx

    content = "".join(
        "data: " + (p if isinstance(p, str) else json.dumps(p)) + "\n\n"
        for p in payloads
    )
    with load_package(
        tmp_path / "sdk", generate_python(event_contract(Event), http_client=backend)
    ) as sdk:
        async with (
            http.AsyncClient(
                transport=http.MockTransport(
                    lambda _: http.Response(
                        200,
                        content=content,
                        headers={"content-type": "text/event-stream"},
                    )
                )
            ) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            events = api.streams.events(
                options=sdk.RequestOptions(unknown_events=policy)
            )
            if expected == "error":
                with pytest.raises(sdk.StreamParseError):
                    await anext(events)
            else:
                result = [event async for event in events]
                assert len(result) == 1 and result[0].type == expected


async def test_ignore_requires_a_discriminated_union(tmp_path):
    with load_package(tmp_path / "sdk", generate_python(event_contract(str))) as sdk:
        async with sdk.Client("http://example.test") as api:
            with pytest.raises(ValueError, match="top-level discriminated"):
                await anext(
                    api.streams.events(
                        options=sdk.RequestOptions(unknown_events="ignore")
                    )
                )


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_ignore_reuses_policy_across_requests_and_keeps_nested_validation(
    tmp_path, backend, monkeypatch
):
    class Container(BaseModel):
        type: Literal["container"]
        child: Event

    event = Annotated[Container | Done, Field(discriminator="type")]
    http = __import__(backend)
    with load_package(
        tmp_path / "sdk", generate_python(event_contract(event), http_client=backend)
    ) as sdk:
        content = (
            'data: {"type":"future","nested":[{"unknown":true}]}\n\n' * 20
            + 'data: {"type":"done","count":1}\n\n'
        )
        async with (
            http.AsyncClient(
                transport=http.MockTransport(
                    lambda _: http.Response(
                        200,
                        content=content,
                        headers={"content-type": "text/event-stream"},
                    )
                )
            ) as client,
            sdk.Client("http://example.test", http_client=client) as api,
        ):
            options = sdk.RequestOptions(unknown_events="ignore")
            assert (
                len([item async for item in api.streams.events(options=options)]) == 1
            )

            def unexpected_schema(*_args, **_kwargs):
                raise AssertionError("Event policy must survive successive requests")

            monkeypatch.setattr(
                api.streams._events_event_adapter, "json_schema", unexpected_schema
            )
            assert (
                len([item async for item in api.streams.events(options=options)]) == 1
            )
            content = 'data: {"type":"container","child":{"type":"future"}}\n\n'
            with pytest.raises(sdk.StreamParseError) as caught:
                await anext(api.streams.events(options=options))
            assert caught.value.category == "invalid_event_schema"
            assert caught.value.validation_errors[0]["loc"] == ("container", "child")


def _typescript_policy(event: object) -> str | None:
    from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript

    api = next(
        text
        for path, text in generate_typescript(event_contract(event)).items()
        if path.endswith("/api.ts")
    )
    line = next((row for row in api.splitlines() if "events: {" in row), None)
    return None if line is None else line.strip()


def test_typescript_operations_declare_known_tags():
    assert _typescript_policy(Event) == (
        'events: {"discriminator": "type", "known": ["done", "started"]},'
    )


def test_typescript_known_tags_cover_aliases_and_multi_value_literals():
    first = create_model(
        "First", kind=(Literal["first", "second"], Field(alias="event-type"))
    )
    last = create_model("Last", kind=(Literal["last"], Field(alias="event-type")))
    event = Annotated[first | last, Field(discriminator="kind")]
    assert _typescript_policy(event) == (
        'events: {"discriminator": "event-type", "known": ["first", "last", "second"]},'
    )


def test_typescript_omits_tags_without_a_discriminated_union():
    assert _typescript_policy(str) is None
    assert _typescript_policy(Started | Done) is None
