import pytest
from fastapi_canon.integrations.sse.codegen.openapi import ContractError
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import TypeAdapter, ValidationError

from tests.codegen.helpers import load_models, schema_contract


@pytest.mark.parametrize("required_first", [False, True])
def test_allof_preserves_required_fields_in_either_order(tmp_path, required_first):
    optional = {"type": "object", "properties": {"x": {"type": "string"}}}
    required = {**optional, "required": ["x"]}
    members = [required, optional] if required_first else [optional, required]
    files = generate_python(schema_contract({"allOf": members}))
    with load_models(tmp_path, files) as models:
        adapter = TypeAdapter(models.EventsEvent)
        assert adapter.validate_python({"x": "ok"}).x == "ok"
        for invalid in ({}, {"x": None}):
            with pytest.raises(ValidationError):
                adapter.validate_python(invalid)


def test_conflicting_allof_fields_fail_generation():
    members = [
        {"type": "object", "properties": {"x": {"type": kind}}}
        for kind in ("string", "integer")
    ]
    with pytest.raises(ContractError, match="Conflicting allOf field: x"):
        generate_python(schema_contract({"allOf": members}))


@pytest.mark.parametrize("closed_first", [False, True])
def test_closed_allof_preserves_forbidden_extras(tmp_path, closed_first):
    shape = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }
    members = [
        {**shape, "additionalProperties": False},
        {**shape, "additionalProperties": True},
    ]
    if not closed_first:
        members.reverse()
    with load_models(
        tmp_path, generate_python(schema_contract({"allOf": members}))
    ) as models:
        adapter = TypeAdapter(models.EventsEvent)
        assert adapter.validate_python({"x": "ok"}).x == "ok"
        with pytest.raises(ValidationError):
            adapter.validate_python({"x": "ok", "extra": 1})


@pytest.mark.parametrize("closed_first", [False, True])
def test_closed_allof_with_new_fields_fails_explicitly(closed_first):
    closed = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
    }
    extension = {"type": "object", "properties": {"y": {"type": "integer"}}}
    members = [closed, extension] if closed_first else [extension, closed]
    with pytest.raises(ContractError, match="Closed-object allOf.*y"):
        generate_python(schema_contract({"allOf": members}))


def test_allof_conflicting_typed_extras_fail_explicitly():
    members = [
        {"type": "object", "additionalProperties": {"type": kind}}
        for kind in ("integer", "string")
    ]
    with pytest.raises(ContractError, match="Conflicting allOf additionalProperties"):
        generate_python(schema_contract({"allOf": members}))
