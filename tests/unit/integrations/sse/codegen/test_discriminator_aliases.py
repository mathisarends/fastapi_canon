import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import TypeAdapter, ValidationError
from tests.unit.integrations.sse.codegen.helpers import load_models, schema_contract


def test_nested_union_alias_keeps_multivalue_discriminators_as_literals(tmp_path):
    schemas = {
        "Event": {
            "oneOf": [
                {"$ref": "#/components/schemas/Action"},
                {"$ref": "#/components/schemas/Waiting"},
            ],
            "discriminator": {"propertyName": "type"},
        },
        "Action": {
            "anyOf": [
                {"$ref": "#/components/schemas/Pointer"},
                {"$ref": "#/components/schemas/Keyboard"},
            ]
        },
        "Pointer": {
            "type": "object",
            "required": ["type", "position"],
            "properties": {
                "type": {"type": "string", "enum": ["move", "click"]},
                "position": {"type": "integer"},
            },
        },
        "Keyboard": {
            "type": "object",
            "required": ["type", "key"],
            "properties": {"type": {"const": "key"}, "key": {"type": "string"}},
        },
        "Waiting": {
            "type": "object",
            "required": ["type"],
            "properties": {"type": {"const": "waiting"}},
        },
    }
    contract = schema_contract({"$ref": "#/components/schemas/Event"}, schemas)
    with load_models(tmp_path, generate_python(contract)) as models:
        adapter = TypeAdapter(models.Event)
        for tag in ("move", "click"):
            item = adapter.validate_python({"type": tag, "position": 5})
            assert isinstance(item, models.Pointer)
            assert item.type == tag
        assert isinstance(
            adapter.validate_python({"type": "key", "key": "a"}), models.Keyboard
        )
        assert isinstance(adapter.validate_python({"type": "waiting"}), models.Waiting)
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "unknown"})
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "click", "key": "a"})
