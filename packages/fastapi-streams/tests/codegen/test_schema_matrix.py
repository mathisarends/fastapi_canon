import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi_canon.integrations.sse.codegen.ir import Reference
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
from fastapi_canon.integrations.sse.codegen.writer import write_files
from pydantic import TypeAdapter, ValidationError

from tests.codegen.helpers import load_models, schema_contract

ROOT = Path(__file__).parents[2]
CASES = [
    ("string", {"type": "string"}, {}, "hello", []),
    ("number", {"type": "number"}, {}, 1.5, {}),
    ("boolean", {"type": "boolean"}, {}, True, "invalid"),
    ("null", {"type": "null"}, {}, None, 1),
    ("array", {"type": "array", "items": {"type": "integer"}}, {}, [1, 2], ["bad"]),
    ("union", {"anyOf": [{"type": "string"}, {"type": "integer"}]}, {}, "ok", []),
    (
        "field_symbols",
        {
            "type": "object",
            "properties": {
                "int": {"type": "integer"},
                "str": {"type": "string"},
                "float": {"type": "number"},
                "bool": {"type": "boolean"},
                "list": {"type": "array", "items": {"type": "string"}},
                "dict": {"type": "object", "additionalProperties": {"type": "string"}},
                "object": {"type": "string"},
                "classmethod": {"type": "string"},
                "field_validator": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {
                        "list": {"type": "array", "items": {"type": "string"}}
                    },
                },
            },
        },
        {},
        {
            "int": 1,
            "str": "x",
            "float": 1.5,
            "bool": False,
            "list": ["x"],
            "dict": {"a": "b"},
            "object": "x",
            "classmethod": "x",
            "field_validator": "x",
            "nested": {"list": ["nested"]},
        },
        {"list": None},
    ),
    (
        "map",
        {"type": "object", "additionalProperties": {"type": "integer"}},
        {},
        {"x": 1},
        {"x": "bad"},
    ),
    (
        "enum",
        {"$ref": "#/components/schemas/Mode"},
        {"Mode": {"type": "string", "enum": ["fast", "full"]}},
        "fast",
        "bad",
    ),
    (
        "object",
        {
            "type": "object",
            "properties": {
                "external-id": {"type": "string"},
                "note": {"type": ["string", "null"]},
                "count": {"type": "integer"},
            },
            "required": ["external-id"],
        },
        {},
        {"external-id": "x", "note": None},
        {"external-id": "x", "count": None},
    ),
    (
        "empty_closed_object",
        {"type": "object", "additionalProperties": False},
        {},
        {},
        {"extra": 1},
    ),
    (
        "forbidden_extras",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        {},
        {"name": "ok"},
        {"name": "ok", "unexpected": 123},
    ),
    (
        "typed_extras",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": {"type": "integer"},
        },
        {},
        {"name": "x", "extra": 1},
        {"name": "x", "extra": "bad"},
    ),
    (
        "annotated_reference_chain",
        {
            "$ref": "#/components/schemas/Alias",
            "description": "An annotated alias",
            "default": "hello",
            "examples": ["hello"],
            "x-display": "name",
        },
        {
            "Alias": {"$ref": "#/components/schemas/Name", "title": "Name alias"},
            "Name": {"type": "string"},
        },
        "hello",
        123,
    ),
    (
        "recursive",
        {"$ref": "#/components/schemas/Node", "description": "A recursive tree"},
        {
            "Node": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "children": {
                        "type": "array",
                        "items": {
                            "$ref": "#/components/schemas/Node",
                            "description": "A child",
                        },
                    },
                },
                "required": ["name", "children"],
            }
        },
        {
            "name": "root",
            "children": [
                {"name": "child", "children": [{"name": "leaf", "children": []}]}
            ],
        },
        {"name": "root", "children": [{"name": None, "children": []}]},
    ),
    (
        "intersection",
        {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
                {
                    "type": "object",
                    "properties": {"y": {"type": "integer"}},
                    "required": ["y"],
                },
            ]
        },
        {},
        {"x": "ok", "y": 1},
        {"x": "ok"},
    ),
]

for policy_name, policy in [
    ("typed", {"type": "string"}),
    ("true", True),
    ("omitted", None),
]:
    for mixed in (False, True):
        schema = {"type": "object", "required": ["name"]}
        valid = {"name": "ok"}
        if mixed:
            schema["properties"] = {"count": {"type": "integer"}}
            schema["required"].append("count")
            valid["count"] = 1
        if policy is not None:
            schema["additionalProperties"] = policy
        CASES.append(
            (
                "required_" + policy_name + ("_mixed" if mixed else ""),
                schema,
                {},
                valid,
                {"count": 1} if mixed else {},
            )
        )


for discriminator, values in [("event-type", ["a"]), ("type", ["a1", "a2"])]:
    CASES.append(
        (
            "discriminator_" + discriminator,
            {
                "oneOf": [
                    {"$ref": "#/components/schemas/A"},
                    {"$ref": "#/components/schemas/B"},
                ],
                "discriminator": {"propertyName": discriminator},
            },
            {
                "A": {
                    "type": "object",
                    "properties": {discriminator: {"type": "string", "enum": values}},
                    "required": [discriminator],
                },
                "B": {
                    "type": "object",
                    "properties": {discriminator: {"type": "string", "const": "b"}},
                    "required": [discriminator],
                },
            },
            {discriminator: values[-1]},
            {discriminator: "unknown"},
        )
    )


@pytest.fixture(scope="module")
def generated_matrix(tmp_path_factory):
    root = tmp_path_factory.mktemp("schema_matrix")
    generated = {}
    for name, schema, components, valid, invalid in CASES:
        contract = schema_contract(schema, components)
        python = generate_python(contract)
        write_files(root / name / "python", python)
        write_files(
            root / name / "python_httpx2",
            generate_python(contract, http_client="httpx2"),
        )
        write_files(root / name / "typescript", generate_typescript(contract))
        event = contract.operations[0].event
        event_name = event.name if isinstance(event, Reference) else "EventsEvent"
        generated[name] = (root / name / "python", python, event_name, valid, invalid)
    return root, generated


@pytest.mark.parametrize("name", [case[0] for case in CASES])
def test_generated_python_validates_supported_schema_matrix(generated_matrix, name):
    _, generated = generated_matrix
    path, files, event_name, valid, invalid = generated[name]
    with load_models(path, files) as models:
        adapter = TypeAdapter(getattr(models, event_name))
        parsed = adapter.validate_python(valid)
        assert (
            json.loads(adapter.dump_json(parsed, by_alias=True, exclude_unset=True))
            == valid
        )
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_every_generated_package_passes_strict_typechecking(generated_matrix):
    root, generated = generated_matrix
    (root / "field_symbols/python/consumer.py").write_text(
        "from .models import EventsEvent\n"
        "event = EventsEvent(int=1, str='x', list=['x'], field_validator='x')\n"
        "values: list[str] | None = event.list_\n"
        "omitted = EventsEvent()\n",
        encoding="utf-8",
    )
    for name, (_, _, _, valid, invalid) in generated.items():
        if not name.startswith("required_"):
            continue
        consumer = (
            'import type { Client } from "./index.js";\n'
            'type Event = ReturnType<Client["streams"]["events"]> '
            "extends AsyncIterable<infer E> ? E : never;\n"
            f"export const valid: Event = {json.dumps(valid)};\n"
            "// @ts-expect-error: required undeclared key must be present\n"
            f"export const missing: Event = {json.dumps(invalid)};\n"
        )
        if name.startswith("required_typed"):
            wrong = {**valid, "name": 123}
            consumer += (
                "// @ts-expect-error: required key uses additionalProperties type\n"
                f"export const wrong: Event = {json.dumps(wrong)};\n"
            )
        (root / name / "typescript/consumer.ts").write_text(consumer, encoding="utf-8")
        for backend in ("python", "python_httpx2"):
            arguments = ", ".join(f"{key}={value!r}" for key, value in valid.items())
            (root / name / backend / "consumer.py").write_text(
                "from .models import EventsEvent\n"
                f"event = EventsEvent({arguments})\n"
                "value: object = event.name\n",
                encoding="utf-8",
            )
    node = shutil.which("node")
    compiler = ROOT / "node_modules/typescript/bin/tsc"
    assert node and compiler.is_file(), (
        "Run npm ci before the generator acceptance tests"
    )
    config = root / "tsconfig.json"
    config.write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "strict": True,
                    "exactOptionalPropertyTypes": True,
                    "target": "ES2022",
                    "module": "ESNext",
                    "moduleResolution": "Bundler",
                    "noEmit": True,
                    "skipLibCheck": True,
                },
                "include": ["**/typescript/**/*.ts"],
            }
        )
    )
    result = subprocess.run(
        [node, str(compiler), "-p", str(config)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    pyright_config = root / "pyrightconfig.json"
    pyright_config.write_text(
        json.dumps(
            {
                "include": ["**/python", "**/python_httpx2"],
                "typeCheckingMode": "strict",
                "pythonVersion": "3.12",
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--project",
            str(pyright_config),
            "--pythonpath",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
