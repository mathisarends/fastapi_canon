import json
from dataclasses import replace

import pytest
from fastapi_canon.integrations.sse.codegen.cli import main
from fastapi_canon.integrations.sse.codegen.diagnostics import accepted_diagnostics
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    resolve_pointer,
)

from tests.codegen.helpers import event_document


def diagnostic_document():
    document = event_document(str)
    operation = document["paths"]["/events"]["get"]
    operation["parameters"] = [
        {
            "name": "cursor",
            "in": "query",
            "schema": {
                "type": "string",
                "format": "uuid",
            },
        }
    ]
    media = operation["responses"]["200"]["content"]["text/event-stream"]
    media["itemSchema"]["properties"]["data"]["contentSchema"] = {
        "$ref": "#/components/schemas/Event",
    }
    document["components"] = {
        "schemas": {
            "Event": {
                "type": "object",
                "properties": {
                    "a/b~c": {"type": "string", "format": "date-time"},
                    "child": {"$ref": "#/components/schemas/Event"},
                },
            }
        }
    }
    return document


def test_inline_and_recursive_ref_diagnostics_have_exact_source_and_stable_identity():
    document = diagnostic_document()
    found = []
    build_ir(document, diagnostics=found)
    assert len(found) == 2
    assert {item.value for item in found} == {"uuid", "date-time"}
    assert any("a~1b~0c/format" in item.pointer for item in found)
    for item in found:
        assert resolve_pointer(document, "#" + item.pointer) == item.value
        assert item.keyword == "format"
        assert (item.operation_id, item.method, item.path, item.target) == (
            "events",
            "GET",
            "/events",
            "python",
        )
        assert item.id == replace(item, message="New wording").id
        assert item.id != replace(item, value="different").id
        for text in (item.pointer, item.value, "events", "GET /events", "python"):
            assert text in str(item)


def test_shared_schema_reports_each_affected_operation_and_skips_unused_schemas():
    document = diagnostic_document()
    document["paths"]["/other"] = {
        "get": {**document["paths"]["/events"]["get"], "operationId": "other"}
    }
    document["components"]["schemas"]["Unused"] = {"type": "string", "format": "email"}
    found = []
    build_ir(document, diagnostics=found)
    assert len(found) == 4
    assert {item.operation_id for item in found} == {"events", "other"}
    assert all(item.value != "email" for item in found)


def test_typescript_diagnoses_supported_python_constraints_without_sse_envelope_noise():
    document = event_document(str)
    document["paths"]["/events"]["get"]["parameters"] = [
        {
            "name": "name",
            "in": "query",
            "schema": {"type": "string", "minLength": 2},
        }
    ]
    python, typescript = [], []
    build_ir(document, diagnostics=python)
    build_ir(document, target="typescript", diagnostics=typescript)
    assert python == []
    assert len(typescript) == 1 and typescript[0].keyword == "minLength"


def test_boolean_schema_refs_and_path_item_overrides_keep_original_locations():
    document = diagnostic_document()
    document["components"]["schemas"]["Event"]["properties"]["anything"] = {
        "$ref": "#/components/schemas/Anything"
    }
    document["components"]["schemas"]["Anything"] = True
    document["components"]["pathItems"] = {"Base": {}}
    document["paths"]["/events"]["$ref"] = "#/components/pathItems/Base"
    found = []
    build_ir(document, diagnostics=found)
    assert len(found) == 2
    for item in found:
        assert resolve_pointer(document, "#" + item.pointer) == item.value


def test_duplicate_allowances_fail():
    found = []
    build_ir(diagnostic_document(), diagnostics=found)
    allowance = {"id": found[0].id, "reason": "Reviewed"}
    with pytest.raises(ContractError, match="Duplicate"):
        accepted_diagnostics(found, {"allow": [allowance, allowance]})


def test_cli_strict_reports_accepts_reviewed_limits_and_rejects_changed_constraints(
    tmp_path,
):
    document = diagnostic_document()
    schema, report, allowances = [
        tmp_path / name for name in ("openapi.json", "report.json", "allow.json")
    ]
    schema.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "sdk"
    flags = [
        "--schema",
        str(schema),
        "--language",
        "python",
        "--output",
        str(output),
        "--strict",
        "--diagnostics",
        str(report),
    ]
    with pytest.warns(UserWarning):
        assert main(["generate", *flags]) == 2
    assert not output.exists()
    diagnostics = json.loads(report.read_text())["diagnostics"]
    assert len(diagnostics) == 2 and not any(item["accepted"] for item in diagnostics)
    allowances.write_text(
        json.dumps(
            {
                "allow": [
                    {
                        "id": item["id"],
                        "reason": "Upstream validates this reviewed format",
                    }
                    for item in diagnostics
                ]
            }
        ),
        encoding="utf-8",
    )
    flags += ["--allow-diagnostics", str(allowances)]
    assert main(["generate", *flags]) == 0
    assert main(["check", *flags]) == 0
    assert all(
        item["accepted"] for item in json.loads(report.read_text())["diagnostics"]
    )
    original = (output / "models.py").read_bytes()
    document["paths"]["/events"]["get"]["parameters"][0]["schema"]["format"] = "email"
    schema.write_text(json.dumps(document), encoding="utf-8")
    assert main(["generate", *flags]) == 2
    assert (output / "models.py").read_bytes() == original
    assert any(
        item["value"] == "email"
        for item in json.loads(report.read_text())["diagnostics"]
    )


@pytest.mark.parametrize(
    "allowances",
    [
        [],
        {},
        {"allow": {}},
        {"allow": [{"id": "unknown", "reason": "reviewed"}]},
        {"allow": [{"id": "unknown", "reason": " "}]},
        {"allow": [{"id": "unknown"}]},
    ],
)
def test_invalid_or_stale_allowances_fail(allowances):
    with pytest.raises(ContractError):
        accepted_diagnostics([], allowances)


def test_diagnostic_report_cannot_overwrite_inputs_or_generated_files(tmp_path):
    schema = tmp_path / "openapi.json"
    original = json.dumps(event_document(str))
    schema.write_text(original, encoding="utf-8")
    for report in (schema, tmp_path / "sdk" / "models.py"):
        assert (
            main(
                [
                    "generate",
                    "--schema",
                    str(schema),
                    "--output",
                    str(tmp_path / "sdk"),
                    "--diagnostics",
                    str(report),
                ]
            )
            == 2
        )
        assert schema.read_text() == original


@pytest.mark.parametrize(
    "target,backend", [("python", "httpx"), ("python", "httpx2"), ("typescript", None)]
)
def test_report_describes_actual_target_phases_and_stable_schema_content(
    tmp_path, target, backend
):
    from fastapi_canon.integrations.sse import __version__

    document = event_document(str)
    schema, report = tmp_path / "openapi.json", tmp_path / "report.json"
    schema.write_text(json.dumps(document), encoding="utf-8")
    flags = [
        "--schema",
        str(schema),
        "--language",
        target,
        "--output",
        str(tmp_path / "sdk"),
        "--diagnostics",
        str(report),
        "--strict",
    ]
    if backend:
        flags += ["--python-http-client", backend]
    assert main(["generate", *flags]) == 0
    original = report.read_bytes()
    result = json.loads(original)
    generation = result["generation"]
    manifest_path = tmp_path / "sdk" / ".fastapi-streams.json"
    manifest_bytes = manifest_path.read_bytes()
    assert json.loads(manifest_bytes)["generation"] == generation
    assert generation["generator"] == "fastapi-canon"
    assert generation["generator_version"] == __version__
    assert generation["target"] == target
    assert generation["python_http_client"] == backend
    assert generation["schema_hash_encoding"] == "canonical-json-v1"
    assert len(generation["schema_sha256"]) == 64
    assert result["version"] == 1 and result["diagnostics"] == []
    validation = result["validation"]
    assert validation["full_json_schema"] is False
    for boundary in ("parameters", "request_body"):
        assert validation[boundary] == {
            "phase": "method_call" if backend else "not_performed",
            "schema_validation": bool(backend),
        }
    assert validation["events"] == {
        "phase": "iteration",
        "json_decoding": True,
        "schema_validation": bool(backend),
    }
    assert validation["http_error_bodies"] == {
        "phase": "consumer_controlled",
        "schema_validation": False,
    }
    # Formatting and key order do not change provenance or report bytes.
    schema.write_text(
        json.dumps(dict(reversed(list(document.items()))), indent=4), encoding="utf-8"
    )
    assert main(["check", *flags]) == 0
    assert report.read_bytes() == original
    assert manifest_path.read_bytes() == manifest_bytes
    # The complete document participates, including components not emitted by SDKs.
    document["info"]["description"] = "Changed contract provenance"
    schema.write_text(json.dumps(document), encoding="utf-8")
    assert main(["check", *flags]) == 1
    assert manifest_path.read_bytes() == manifest_bytes
    assert (
        json.loads(report.read_bytes())["generation"]["schema_sha256"]
        != generation["schema_sha256"]
    )
    assert main(["generate", *flags]) == 0
    assert (
        json.loads(manifest_path.read_bytes())["generation"]
        == json.loads(report.read_bytes())["generation"]
    )
