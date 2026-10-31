import json
from pathlib import Path

import pytest
from fastapi_canon.integrations.sse.codegen.cli import main
from fastapi_canon.integrations.sse.codegen.ir import Reference, Union, build_ir
from fastapi_canon.integrations.sse.codegen.openapi import ContractError, export_schema
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
from fastapi_canon.integrations.sse.codegen.writer import MANIFEST, write_files

from examples.agent_runs import app


@pytest.mark.parametrize(
    "target,backend", [("python", "httpx"), ("python", "httpx2"), ("typescript", None)]
)
def test_cli_upgrades_legacy_manifest_without_a_diagnostic_report(
    tmp_path, target, backend
):
    schema, output = tmp_path / "schema.json", tmp_path / "sdk"
    document = export_schema(app)
    schema.write_text(json.dumps(document), encoding="utf-8")
    write_files(output, {"stale.txt": "old"})
    handwritten = output / "handwritten.txt"
    handwritten.write_text("keep", encoding="utf-8")
    flags = ["--schema", str(schema), "--language", target, "--output", str(output)]
    if backend:
        flags += ["--python-http-client", backend]
    legacy = (output / MANIFEST).read_bytes()
    assert main(["check", *flags]) == 1
    assert (output / MANIFEST).read_bytes() == legacy
    assert main(["generate", *flags]) == 0
    manifest = json.loads((output / MANIFEST).read_bytes())
    assert manifest["generation"]["target"] == target
    assert manifest["generation"]["python_http_client"] == backend
    assert manifest["files"] == sorted(
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name not in {MANIFEST, "handwritten.txt"}
    )
    assert not (output / "stale.txt").exists()
    assert handwritten.read_text() == "keep"
    assert main(["check", *flags]) == 0
    # A different generator version is drift even if source files are identical.
    manifest["generation"]["generator_version"] = "0.0.1"
    (output / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    stale = (output / MANIFEST).read_bytes()
    assert main(["check", *flags]) == 1
    assert (output / MANIFEST).read_bytes() == stale


def test_ir_and_types_are_typed_and_streams_only():
    contract = build_ir(export_schema(app))
    assert {op.operation_id for op in contract.operations} == {
        "stream_run_events",
        "stream_prompt",
    }
    assert all(op.group == "agentRuns" for op in contract.operations)
    event = next(
        declaration
        for declaration in contract.declarations
        if declaration.name == "RunEvent"
    )
    assert isinstance(event.type, Union)
    assert event.type.discriminator == "type"
    assert all(isinstance(member, Reference) for member in event.type.members)
    files = generate_typescript(contract)
    assert files == generate_typescript(build_ir(export_schema(app)))
    assert '"token.delta"' in files["types.ts"]
    assert "AsyncIterable<Types.RunEvent>" in files["agent-runs/api.ts"]
    assert "health" not in files["client.ts"]
    assert "any" not in files["types.ts"]
    assert "unknown" not in files["types.ts"]
    assert contract.security_schemes[0].kind == "http"


def test_cli_export_generate_and_check(tmp_path):
    schema = tmp_path / "schema.json"
    output = tmp_path / "sdk"
    args = ["schema", "--app", "examples.agent_runs:app", "--output", str(schema)]
    assert main(args + ["--check"]) == 1
    assert not schema.exists()
    assert main(args) == 0
    assert main(args + ["--check"]) == 0
    args = ["generate", "--schema", str(schema), "--output", str(output)]
    assert main(args + ["--check"]) == 1
    assert not output.exists()
    assert main(args) == 0
    assert main(args + ["--check"]) == 0
    (output / "types.ts").write_text("stale")
    assert main(args + ["--check"]) == 1
    assert main(args) == 0
    assert main(args + ["--check"]) == 0


def test_writer_removes_only_owned_files_and_checks_stale(tmp_path):
    write_files(tmp_path, {"old/api.ts": "old"})
    (tmp_path / "handwritten.ts").write_text("keep")
    assert tmp_path / "old/api.ts" in write_files(
        tmp_path, {"new.ts": "new"}, check=True
    )
    assert (tmp_path / "old/api.ts").exists()
    write_files(tmp_path, {"new.ts": "new"})
    assert not (tmp_path / "old/api.ts").exists()
    assert (tmp_path / "handwritten.ts").read_text() == "keep"
    assert not write_files(tmp_path, {"new.ts": "new"}, check=True)


def test_writer_preflights_unsafe_manifest_before_any_write(tmp_path):
    (tmp_path / MANIFEST).write_text(json.dumps({"files": ["../outside.ts"]}))
    with pytest.raises(ValueError, match="Unsafe"):
        write_files(tmp_path, {"safe.ts": "new"})
    assert not (tmp_path / "safe.ts").exists()


@pytest.mark.parametrize(
    "unsafe", ["../outside.ts", "/absolute.ts", "C:/drive.ts", "..\\outside.ts"]
)
def test_writer_rejects_unsafe_new_paths(tmp_path, unsafe):
    with pytest.raises(ValueError):
        write_files(tmp_path, {unsafe: "no"})


def test_generator_rejects_untyped_streams_and_name_collisions():
    document = export_schema(app)
    operation = document["paths"]["/v1/runs/{run_id}/events"]["get"]
    operation["responses"]["200"]["content"]["text/event-stream"]["itemSchema"] = {}
    with pytest.raises(ContractError, match="contentSchema"):
        build_ir(document)
    document = export_schema(app)
    document["paths"]["/v1/runs/{run_id}/events"]["post"]["operationId"] = (
        "stream_run_events"
    )
    with pytest.raises(ContractError, match="duplicate"):
        build_ir(document)


@pytest.mark.snapshot
def test_example_contract_snapshot():
    snapshot = Path(__file__).parents[1] / "fixtures/agent_runs.openapi.json"
    assert json.loads(snapshot.read_text(encoding="utf-8")) == export_schema(app)


@pytest.mark.parametrize("language", ["typescript", "python"])
def test_check_command_detects_both_contract_and_client_drift(tmp_path, language):
    schema = tmp_path / "openapi.json"
    output = tmp_path / "sdk"
    assert (
        main(["schema", "--app", "examples.agent_runs:app", "--output", str(schema)])
        == 0
    )
    flags = ["--schema", str(schema), "--language", language, "--output", str(output)]
    assert main(["generate", *flags]) == 0
    check = ["check", "--app", "examples.agent_runs:app", *flags]
    assert main(check) == 0
    model = output / ("types.ts" if language == "typescript" else "models.py")
    model.write_text("stale")
    assert main(check) == 1
    assert model.read_text() == "stale"
    assert main(["generate", *flags]) == 0
    document = json.loads(schema.read_text())
    document["info"]["title"] = "old title"
    schema.write_text(json.dumps(document))
    before = schema.read_bytes()
    assert main(check) == 1
    assert schema.read_bytes() == before
