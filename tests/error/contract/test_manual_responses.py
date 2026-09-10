import pytest
from fastapi import APIRouter, FastAPI

from fastapi_canon.error import ErrorConfigurationError, ErrorRegistry


@pytest.mark.parametrize(
    "response",
    [
        {
            "description": "Server-sent event stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        {
            "description": "PDF document",
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        {"description": "Service is ready"},
    ],
)
def test_manual_responses_without_canon_metadata_are_preserved(
    response: dict[str, object],
) -> None:
    router = APIRouter()
    router.get("/resource", responses={200: response})(lambda: None)
    app = FastAPI()
    app.include_router(router)
    registry = ErrorRegistry(
        errors=[],
        type_base="https://example.test/problems",
    )

    registry.install(app)
    documented = app.openapi()["paths"]["/resource"]["get"]["responses"]["200"]

    assert documented["description"] == response["description"]
    content = response.get("content")
    if isinstance(content, dict):
        for media_type, media_contract in content.items():
            assert documented["content"][media_type] == media_contract
    assert "x-fastapi-canon-errors" not in documented


def test_explicit_malformed_canon_metadata_is_still_rejected() -> None:
    router = APIRouter()
    router.get(
        "/resource",
        responses={
            200: {
                "description": "Invalid metadata",
                "x-fastapi-canon-errors": (),
            }
        },
    )(lambda: None)
    app = FastAPI()
    app.include_router(router)
    registry = ErrorRegistry(
        errors=[],
        type_base="https://example.test/problems",
    )

    with pytest.raises(ErrorConfigurationError, match="invalid fastapi-canon"):
        registry.install(app)
