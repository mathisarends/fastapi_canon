import copy

import pytest
from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    export_schema,
    validate_contract,
)

from examples.agent_runs import app


def test_native_contract_is_valid_and_preserves_rest():
    native = copy.deepcopy(app.openapi())
    contract = export_schema(app)
    assert contract["openapi"] == "3.2.0"
    assert app.openapi() == native
    assert contract["paths"]["/health"] == native["paths"]["/health"]
    operation = contract["paths"]["/v1/runs/{run_id}/events"]["get"]
    assert operation["operationId"] == "stream_run_events"
    content = operation["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}
    envelope = content["text/event-stream"]["itemSchema"]
    assert envelope["required"] == ["data"]
    data = envelope["properties"]["data"]
    assert data["contentMediaType"] == "application/json"
    payload = data["contentSchema"]
    if "$ref" in payload:
        payload = contract["components"]["schemas"][payload["$ref"].split("/")[-1]]
    assert len(payload["oneOf"]) == 4
    assert payload["discriminator"]["propertyName"] == "type"
    assert "run.completed" in payload["discriminator"]["mapping"]
    validate_contract(contract)


@pytest.mark.parametrize("mutation", ["version", "structure", "schema", "reference"])
def test_validation_catches_invalid_contracts(mutation):
    contract = export_schema(app)
    if mutation == "version":
        contract["openapi"] = "3.1.0"
    elif mutation == "structure":
        contract["paths"]["/health"]["get"]["responses"] = []
    elif mutation == "schema":
        contract["components"]["schemas"]["Invalid"] = {"type": "nonsense"}
    else:
        contract["components"]["schemas"]["Invalid"] = {"$ref": "#/missing"}
    with pytest.raises(ContractError):
        validate_contract(contract)
