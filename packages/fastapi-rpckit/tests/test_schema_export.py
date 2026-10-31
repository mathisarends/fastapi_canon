import json

import pytest
from fastapi_canon.integrations.websockets.schema.export import (
    ProtocolReferenceError,
    load_contract_source,
    load_protocol,
    render_contract,
)

from tests.test_contract import contract

REFERENCE = "tests.test_contract:contract"


def test_contract_reference_resolves() -> None:
    assert load_contract_source(REFERENCE) is contract
    assert load_protocol(REFERENCE) is contract.protocol


def test_channel_and_service_references_are_rejected() -> None:
    with pytest.raises(ProtocolReferenceError, match="RpcChannel"):
        load_contract_source("tests.test_contract:channel")
    with pytest.raises(ProtocolReferenceError, match="RpcService"):
        load_contract_source("tests.test_contract:service")


def test_contract_is_rendered_as_json() -> None:
    document = json.loads(render_contract(contract))
    assert document["info"]["title"] == "Control API"
    assert document["servers"][0]["name"] == "rpc"
