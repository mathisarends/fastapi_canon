import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import TypeAdapter, ValidationError

from tests.codegen.helpers import load_models, schema_contract


@pytest.mark.parametrize("policy", ["omitted", True, {"type": "string"}])
@pytest.mark.parametrize("mixed", [False, True])
def test_required_undeclared_keys_validate_with_the_additional_value_type(
    tmp_path, policy, mixed
):
    schema = {"type": "object", "required": ["name"]}
    if mixed:
        schema["properties"] = {"count": {"type": "integer"}}
    if policy != "omitted":
        schema["additionalProperties"] = policy
    with load_models(tmp_path, generate_python(schema_contract(schema))) as models:
        adapter = TypeAdapter(models.EventsEvent)
        assert adapter.validate_python({"name": "ok"}).name == "ok"
        with pytest.raises(ValidationError):
            adapter.validate_python({"count": 1} if mixed else {})
        if isinstance(policy, dict):
            for invalid in (
                {"name": 123},
                {"name": None},
                {"name": "ok", "extra": 123},
            ):
                with pytest.raises(ValidationError):
                    adapter.validate_python(invalid)
        else:
            for value in (None, 123, {"nested": [True]}):
                assert adapter.validate_python({"name": value}).name == value


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {"type": "object", "additionalProperties": True},
        {"type": "object", "additionalProperties": {"type": "string"}},
    ],
)
def test_maps_without_required_keys_still_allow_empty_objects(tmp_path, schema):
    with load_models(tmp_path, generate_python(schema_contract(schema))) as models:
        assert TypeAdapter(models.EventsEvent).validate_python({}) == {}
