import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
from fastapi_canon.integrations.sse.codegen.writer import write_files
from pydantic import ValidationError

from tests.codegen.helpers import load_models, schema_contract


def test_single_constant_fields_remain_literals_and_preserve_python_defaults(tmp_path):
    contract = schema_contract(
        {"$ref": "#/components/schemas/Problem"},
        {
            "Problem": {
                "type": "object",
                "properties": {
                    "title": {"const": "Not found", "default": "Not found"},
                    "type": {"const": "urn:example:not_found"},
                    "decision": {"enum": ["approve", "reject"]},
                    "named": {"$ref": "#/components/schemas/SingleChoice"},
                },
                "required": ["type", "decision", "named"],
            },
            "SingleChoice": {"type": "string", "enum": ["only"]},
        },
    )
    typescript = generate_typescript(contract)["types.ts"]
    assert 'title?: "Not found";' in typescript
    assert 'type: "urn:example:not_found";' in typescript
    assert "ProblemTitle" not in typescript
    assert "ProblemType" not in typescript
    assert "export const ProblemDecision" in typescript
    assert "export const SingleChoice" in typescript
    with load_models(tmp_path, generate_python(contract)) as models:
        problem = models.Problem(
            type="urn:example:not_found", decision="approve", named="only"
        )
        assert type(problem.title) is str
        assert problem.title == "Not found"
        assert "title" not in problem.model_dump(exclude_unset=True)
        assert problem.decision is models.ProblemDecision.APPROVE
        assert problem.named is models.SingleChoice.ONLY
        assert not hasattr(models, "ProblemTitle")
        with pytest.raises(ValidationError):
            models.Problem(
                type="urn:example:not_found",
                decision="approve",
                named="only",
                title=None,
            )


def test_readable_typescript_preserves_union_intersection_precedence(tmp_path):
    contract = schema_contract(
        {
            "allOf": [
                {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                {"type": "string"},
            ],
        }
    )
    files = generate_typescript(contract)
    write_files(tmp_path, files)
    (tmp_path / "consumer.ts").write_text(
        'import type { Client } from "./index.js";\n'
        'type Event = ReturnType<Client["streams"]["events"]> '
        "extends AsyncIterable<infer E> ? E : never;\n"
        'export const valid: Event = "text";\n'
        "// @ts-expect-error: number is excluded by the intersection\n"
        "export const invalid: Event = 42;\n",
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "strict": True,
                    "target": "ES2022",
                    "module": "ESNext",
                    "moduleResolution": "Bundler",
                    "skipLibCheck": True,
                    "noEmit": True,
                },
                "include": ["**/*.ts"],
            }
        ),
        encoding="utf-8",
    )
    compiler = Path(__file__).parents[2] / "node_modules/typescript/bin/tsc"
    node = shutil.which("node")
    assert node and compiler.is_file(), "Run npm ci before acceptance tests"
    result = subprocess.run(
        [node, str(compiler), "-p", str(tmp_path / "tsconfig.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
