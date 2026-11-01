import pytest
from pydantic import ValidationError

from fastapi_canon.error import Problem


def test_problem_serializes_flat_extensions_and_omits_none() -> None:
    problem = Problem.model_validate(
        {
            "type": "https://api.example.com/problems/session_not_found",
            "title": "Session not found",
            "status": 404,
            "code": "session_not_found",
            "session_id": "0198e2ef-799c-765d-9ab4-2130d11d1e70",
        }
    )

    assert problem.as_dict() == {
        "type": "https://api.example.com/problems/session_not_found",
        "title": "Session not found",
        "status": 404,
        "code": "session_not_found",
        "session_id": "0198e2ef-799c-765d-9ab4-2130d11d1e70",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "/relative"),
        ("type", "contains whitespace"),
        ("type", "http://[::1"),
        ("type", "https:///missing-host"),
        ("type", "https://api.example.com/problem#fragment"),
        ("title", ""),
        ("title", "two\nlines"),
        ("status", 99),
        ("status", 600),
        ("code", "NOPE"),
        ("instance", "contains whitespace"),
        ("instance", "http://[::1"),
    ],
)
def test_problem_rejects_invalid_standard_members(field: str, value: object) -> None:
    data: dict[str, object] = {
        "type": "https://api.example.com/problems/example",
        "title": "Example problem",
        "status": 400,
        "code": "example_problem",
    }
    data[field] = value

    with pytest.raises(ValidationError):
        Problem.model_validate(data)


def test_problem_accepts_relative_instance_uri_reference() -> None:
    problem = Problem(
        type="urn:example:problem",
        title="Example problem",
        status=400,
        code="example_problem",
        instance="/requests/123",
    )

    assert problem.instance == "/requests/123"


def test_problem_rejects_non_json_extension_values() -> None:
    with pytest.raises(ValidationError):
        Problem.model_validate(
            {
                "type": "urn:example:problem",
                "title": "Example problem",
                "status": 400,
                "code": "example_problem",
                "invalid": object(),
            }
        )


@pytest.mark.parametrize("value", [float("inf"), {"nested": object()}])
def test_problem_rejects_invalid_nested_json_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Problem.model_validate(
            {
                "type": "urn:example:problem",
                "title": "Example problem",
                "status": 400,
                "code": "example_problem",
                "invalid": value,
            }
        )
