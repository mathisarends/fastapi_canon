from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest

from examples.canonical_errors.app import contract
from fastapi_canon.codegen.config import (
    Configuration,
    SourceConfig,
    TargetConfig,
    load_config,
)
from fastapi_canon.codegen.generate import generate
from fastapi_canon.codegen.ir import GenerationError
from fastapi_canon.codegen.planner import build_plan
from fastapi_canon.codegen.render import render


def http_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Records", "version": "1"},
        "paths": {
            "/records": {
                "get": {
                    "operationId": "records.list",
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Record"}
                                }
                            },
                        }
                    },
                },
            },
            "/upload": {
                "post": {
                    "operationId": "records.upload",
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {"schema": {"type": "object"}}
                        }
                    },
                    "responses": {"204": {"description": "Saved"}},
                },
            },
        },
        "components": {
            "schemas": {
                "Record": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Child"}},
                },
                "Child": {
                    "type": "object",
                    "properties": {"parent": {"$ref": "#/components/schemas/Record"}},
                },
                "Unused": {"not": {"type": "string"}},
            }
        },
    }


@pytest.mark.parametrize("include", [True, False])
@pytest.mark.parametrize("language", ["python", "typescript"])
def test_selected_http_operations_and_recursive_model_closure(
    tmp_path: Path, include: bool, language: Literal["python", "typescript"]
) -> None:
    document = http_document()
    original = copy.deepcopy(document)
    source = SourceConfig(
        "api",
        "openapi",
        include_operations=("records.list",) if include else None,
        exclude_operations=() if include else ("records.upload",),
        operations={"records.upload": "files.upload"},
    )
    plan = build_plan(((source, document),))
    assert [operation.name for operation in plan.operations] == [("records", "list")]
    assert {model.name for model in plan.models} == {"Record", "Child"}
    files = render(plan, TargetConfig("test", language, ("api",), tmp_path))
    assert files
    assert document == original
    with pytest.raises(GenerationError, match="only JSON"):
        build_plan(((SourceConfig("api", "openapi"), document),))


@pytest.mark.parametrize("kind", ["openrpc", "webrtc"])
def test_selection_applies_to_binding_emitters(
    tmp_path: Path, kind: Literal["openrpc", "webrtc"]
) -> None:
    if kind == "openrpc":
        document = contract.to_openrpc()
        document["x-rpc-notifications"] = [
            {
                "name": "records.updates",
                "payload": {"type": "integer"},
                "message": {"type": "integer"},
            }
        ]
        document["x-rpckit-sockets"] = [
            {
                "name": "records.session",
                "url": "wss://test/socket",
                "messageTypes": ["text", "binary"],
            }
        ]
        keep = "projects.cancel"
    else:
        session: dict[str, Any] = {
            "path": "/session",
            "role": "answerer",
            "description": None,
            "parameters": [],
            "media": {"audio": None, "video": None, "max_incoming_tracks": 4},
            "channels": [],
        }
        document = {
            "contract_version": 1,
            "protocol": 1,
            "subprotocol": "fastapi-webrtc.v1",
            "definitions": {},
            "sessions": [
                dict(session, name="room"),
                dict(session, name="preview", path="/preview"),
            ],
        }
        keep = "room"
    source = SourceConfig("source", kind, include_operations=(keep,))
    plan = build_plan(((source, document),))
    assert len(plan.operations) == 1
    assert plan.operations[0].binding.operation == keep
    files = render(plan, TargetConfig("test", "typescript", ("source",), tmp_path))
    internal_clients = "\n".join(
        value for name, value in files.items() if name.endswith("client.ts")
    )
    assert "preview" not in internal_clients
    assert "records.updates" not in internal_clients


def test_selection_keeps_refs_in_fields_named_like_schema_annotations() -> None:
    document = http_document()
    models = document["components"]["schemas"]
    for name in ("default", "example", "examples"):
        models["Record"]["properties"][name] = {"$ref": "#/components/schemas/Metadata"}
    models["Metadata"] = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
    }
    source = SourceConfig("api", "openapi", include_operations=("records.list",))
    plan = build_plan(((source, document),))
    assert {model.name for model in plan.models} == {"Record", "Child", "Metadata"}


@pytest.mark.parametrize(
    "settings",
    [
        {"include_operations": ("typo",)},
        {"exclude_operations": ("typo",)},
        {
            "include_operations": ("records.list",),
            "exclude_operations": ("records.list",),
        },
        {"include_operations": ()},
        {"exclude_operations": ("records.list", "records.upload")},
    ],
)
def test_unknown_conflicting_and_empty_selections_fail(
    settings: dict[str, Any],
) -> None:
    with pytest.raises(GenerationError):
        build_plan(((SourceConfig("api", "openapi", **settings), http_document()),))


@pytest.mark.parametrize(
    "setting",
    [
        "include-operations = []",
        'include-operations = "records.list"',
        'exclude-operations = ["records.list", "records.list"]',
        "exclude-operations = [1]",
    ],
)
def test_invalid_selection_configuration(tmp_path: Path, setting: str) -> None:
    config = tmp_path / "fastapi-canon.toml"
    config.write_text(
        f"""[codegen]
version = 1
[codegen.sources.api]
kind = "openapi"
input = "api.json"
{setting}
[codegen.targets.python]
language = "python"
sources = ["api"]
output = "generated"
""",
        encoding="utf-8",
    )
    with pytest.raises(GenerationError, match="operations requires"):
        load_config(config)


def test_selection_records_original_provenance_and_is_drift_checked(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "api.json"
    contents = json.dumps(http_document())
    contract.write_text(contents, encoding="utf-8")
    source = SourceConfig(
        "api", "openapi", input=contract, exclude_operations=("records.upload",)
    )
    config = Configuration(
        tmp_path / "fastapi-canon.toml",
        (source,),
        (TargetConfig("python", "python", ("api",), tmp_path / "sdk"),),
    )
    assert generate(config, offline=True)[0]
    assert not generate(config, offline=True, check=True)[0]
    assert contract.read_text(encoding="utf-8") == contents
    manifest = json.loads(
        (tmp_path / "sdk/.canon-codegen.json").read_text(encoding="utf-8")
    )
    assert manifest["mappings"]["api"]["excludeOperations"] == ["records.upload"]
    # Equivalent selection still changes provenance, even with identical SDK methods.
    changed = replace(
        source, include_operations=("records.list",), exclude_operations=()
    )
    assert generate(replace(config, sources=(changed,)), offline=True, check=True)[0]
