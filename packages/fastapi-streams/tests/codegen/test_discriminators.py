from typing import Annotated, Literal

import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from pydantic import Field, TypeAdapter, ValidationError, create_model

from tests.codegen.helpers import event_contract, load_models


@pytest.mark.parametrize(
    "alias", ["event-type", "eventType", "class", "list", "field_validator"]
)
def test_discriminator_uses_python_field_name_and_preserves_wire_alias(tmp_path, alias):
    first = create_model("First", kind=(Literal["first"], Field(alias=alias)))
    second = create_model("Second", kind=(Literal["second"], Field(alias=alias)))
    event = Annotated[first | second, Field(discriminator="kind")]
    files = generate_python(event_contract(event))
    with load_models(tmp_path, files) as models:
        adapter = TypeAdapter(models.EventsEvent)
        for value in ("first", "second"):
            parsed = adapter.validate_python({alias: value})
            assert parsed.model_dump(by_alias=True) == {alias: value}
        with pytest.raises(ValidationError):
            adapter.validate_python({alias: "unknown"})


@pytest.mark.parametrize("optional", [False, True])
def test_multiple_discriminator_values_remain_literals(tmp_path, optional):
    default = "second" if optional else ...
    first = create_model("First", kind=(Literal["first", "second"], default))
    last = create_model("Last", kind=(Literal["last"], ...))
    event = Annotated[first | last, Field(discriminator="kind")]
    with load_models(tmp_path, generate_python(event_contract(event))) as models:
        adapter = TypeAdapter(models.EventsEvent)
        for value in ("first", "second", "last"):
            parsed = adapter.validate_python({"kind": value})
            assert parsed.kind is getattr(models.EventsEventKind, value.upper())
        if optional:
            assert models.First().kind is models.EventsEventKind.SECOND
            assert models.First().model_dump(exclude_unset=True) == {}
        with pytest.raises(ValidationError):
            adapter.validate_python({"kind": "unknown"})
