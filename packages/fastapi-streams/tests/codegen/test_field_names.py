import pytest
from fastapi_canon.integrations.sse.codegen.openapi import ContractError
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import ValidationError, create_model

from tests.codegen.helpers import event_contract, load_package, schema_contract


@pytest.mark.parametrize(
    ("name", "type_", "default", "value"),
    [
        ("int", int, 1, 2),
        ("str", str, "x", "y"),
        ("list", list[str], [], ["y"]),
        ("field_validator", str, "x", "y"),
    ],
)
def test_optional_symbol_fields_import_and_preserve_wire_names(
    tmp_path, name, type_, default, value
):
    source = create_model("Event", **{name: (type_, default)})
    with load_package(tmp_path, generate_python(event_contract(source))) as sdk:
        model = sdk.models.Event
        assert model().model_dump(by_alias=True, exclude_unset=True) == {}
        assert model.model_validate({name: value}).model_dump(by_alias=True) == {
            name: value
        }
        with pytest.raises(ValidationError):
            model.model_validate({name: None})


def test_escaped_field_collisions_fail_explicitly():
    contract = schema_contract(
        {
            "type": "object",
            "properties": {"list": {"type": "string"}, "list_": {"type": "string"}},
        }
    )
    with pytest.raises(ContractError, match="Python field name collision.*list_"):
        generate_python(contract)
