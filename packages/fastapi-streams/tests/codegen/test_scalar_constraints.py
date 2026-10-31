import pytest
from fastapi_canon.integrations.sse.codegen.diagnostics import (
    UnsupportedConstraintWarning,
)
from fastapi_canon.integrations.sse.codegen.openapi import ContractError
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import TypeAdapter, ValidationError

from tests.codegen.helpers import load_models, schema_contract


@pytest.mark.parametrize(
    ("schema", "valid", "invalid"),
    [
        ({"type": "string", "pattern": "^run_[A-Za-z0-9_-]+$"}, "run_abc", "bad"),
        ({"type": "string", "minLength": 2, "maxLength": 4}, "abc", "a"),
        ({"type": "string", "maxLength": 4}, "abc", "abcde"),
        ({"type": "number", "minimum": 2, "maximum": 4}, 3, 1),
        ({"type": "number", "exclusiveMinimum": 2, "exclusiveMaximum": 4}, 3, 4),
        ({"type": "integer", "multipleOf": 3}, 6, 5),
    ],
)
def test_constraints_survive_nested_refs_and_arrays(tmp_path, schema, valid, invalid):
    contract = schema_contract(
        {"$ref": "#/components/schemas/Event"},
        {
            "Value": schema,
            "Event": {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "values": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Value"},
                    }
                },
            },
        },
    )
    with load_models(tmp_path, generate_python(contract)) as models:
        assert models.Event(values=[valid]).values == [valid]
        with pytest.raises(ValidationError):
            models.Event(values=[invalid])


def test_nullable_string_constraints_do_not_reject_null(tmp_path):
    contract = schema_contract({"type": ["string", "null"], "minLength": 3})
    with load_models(tmp_path, generate_python(contract)) as models:
        adapter = TypeAdapter(models.EventsEvent)
        assert adapter.validate_python(None) is None
        with pytest.raises(ValidationError):
            adapter.validate_python("ab")


def test_unsupported_constraints_are_diagnosed():
    with pytest.warns(UnsupportedConstraintWarning, match="uniqueItems"):
        schema_contract(
            {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
        )
    with pytest.raises(ContractError, match="Scalar constraints alongside"):
        schema_contract({"type": "string", "enum": ["a"], "minLength": 2})


def test_unsupported_regex_fails_generation():
    contract = schema_contract({"type": "string", "pattern": "(?=x)x"})
    with pytest.raises(ContractError, match="Unsupported string pattern"):
        generate_python(contract)
