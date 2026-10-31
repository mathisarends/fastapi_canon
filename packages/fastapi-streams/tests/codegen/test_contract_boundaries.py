import json
from copy import deepcopy

import pytest
from fastapi_canon.integrations.sse.codegen.cli import main
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import ContractError
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript

from tests.codegen.helpers import event_document, schema_contract


@pytest.mark.parametrize(
    ("schema", "components", "message"),
    [
        ({"$ref": "https://example.test/schema.json"}, {}, "Only local"),
        ({"$ref": "#/components/schemas/Missing"}, {}, "Unresolved reference"),
        (
            {"$ref": "#/components/schemas/A"},
            {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            },
            "Cyclic reference",
        ),
        ({"type": "array", "prefixItems": [{"type": "integer"}]}, {}, "Tuple schemas"),
        ({"const": {"x": 1}}, {}, "Object and array"),
        (
            {"$ref": "#/components/schemas/Event"},
            {
                "Event": {
                    "type": "object",
                    "properties": {"kind": {"enum": ["a-b", "a_b"]}},
                }
            },
            "Enum member names collide",
        ),
    ],
)
def test_unsupported_schema_contracts_fail_explicitly(schema, components, message):
    with pytest.raises(ContractError, match=message):
        schema_contract(schema, components)


@pytest.mark.parametrize(
    "case",
    [
        "cookie",
        "deep_object",
        "reserved_query",
        "multipart",
        "put",
        "multiple_success",
        "missing_id",
        "duplicate_name",
        "reserved_tag",
    ],
)
def test_unsupported_http_contracts_fail_explicitly(case):
    document = event_document(str)
    operation = document["paths"]["/events"]["get"]
    if case in {"cookie", "deep_object", "reserved_query"}:
        parameter = {"name": "value", "in": "query", "schema": {"type": "string"}}
        if case == "cookie":
            parameter["in"] = "cookie"
        elif case == "deep_object":
            parameter["style"] = "deepObject"
        else:
            parameter["allowReserved"] = True
        operation["parameters"] = [parameter]
    elif case == "multipart":
        document["paths"]["/events"] = {"post": operation}
        operation["requestBody"] = {
            "content": {"multipart/form-data": {"schema": {"type": "object"}}}
        }
    elif case == "put":
        document["paths"]["/events"] = {"put": operation}
    elif case == "multiple_success":
        operation["responses"]["201"] = deepcopy(operation["responses"]["200"])
    elif case == "missing_id":
        del operation["operationId"]
    elif case == "duplicate_name":
        other = deepcopy(operation)
        operation["operationId"], other["operationId"] = "a-b", "a_b"
        document["paths"]["/other"] = {"get": other}
    else:
        operation["tags"] = ["transport"]
    with pytest.raises(ContractError):
        build_ir(document)


@pytest.mark.parametrize(
    ("name", "render"),
    [("Client", generate_typescript), ("BaseModel", generate_python)],
)
def test_reserved_public_model_names_fail_generation(name, render):
    contract = schema_contract(
        {"$ref": f"#/components/schemas/{name}"},
        {name: {"type": "object", "properties": {"x": {"type": "string"}}}},
    )
    with pytest.raises(ContractError, match="Reserved"):
        render(contract)


@pytest.mark.parametrize(
    "sibling",
    [
        {"enum": ["allowed"]},
        {"const": "allowed"},
        {"required": ["name"]},
        {"additionalProperties": False},
        {"minLength": 3},
        {"properties": {"name": {"type": "string"}}},
        {"type": "string"},
        {"allOf": [{"type": "string"}]},
    ],
)
@pytest.mark.parametrize("chained", [False, True])
def test_semantic_ref_siblings_fail_explicitly(sibling, chained):
    target = (
        {"type": "object"}
        if "required" in sibling
        or "additionalProperties" in sibling
        or "properties" in sibling
        else {"type": "string"}
    )
    schema = {"$ref": "#/components/schemas/Target", **sibling}
    components = {"Target": target}
    if chained:
        components["Alias"] = schema
        schema = {"$ref": "#/components/schemas/Alias"}
    with pytest.raises(ContractError, match=r"alongside \$ref.*" + next(iter(sibling))):
        schema_contract(schema, components)


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_ref_sibling_cli_errors_do_not_write_partial_output(tmp_path, capsys, language):
    document = event_document(str)
    document["components"] = {"schemas": {"Name": {"type": "string"}}}
    media = document["paths"]["/events"]["get"]["responses"]["200"]["content"]
    media["text/event-stream"]["itemSchema"]["properties"]["data"]["contentSchema"] = {
        "$ref": "#/components/schemas/Name",
        "enum": ["allowed"],
    }
    schema = tmp_path / "openapi.json"
    schema.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "sdk"
    assert (
        main(
            [
                "generate",
                "--schema",
                str(schema),
                "--language",
                language,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "alongside $ref" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("mixed", [False, True])
def test_forbidden_required_undeclared_keys_fail_before_writing(
    tmp_path, capsys, language, mixed
):
    schema = {"type": "object", "required": ["name"], "additionalProperties": False}
    if mixed:
        schema["properties"] = {"count": {"type": "integer"}}
    with pytest.raises(ContractError, match="Required.*name.*additionalProperties"):
        schema_contract(schema)
    document = event_document(str)
    media = document["paths"]["/events"]["get"]["responses"]["200"]["content"]
    media["text/event-stream"]["itemSchema"]["properties"]["data"]["contentSchema"] = (
        schema
    )
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "sdk"
    assert (
        main(
            [
                "generate",
                "--schema",
                str(path),
                "--language",
                language,
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "additionalProperties" in capsys.readouterr().err
    assert not output.exists()
