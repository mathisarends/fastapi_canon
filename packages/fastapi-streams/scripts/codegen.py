import sys
from pathlib import Path

from fastapi_canon.integrations.sse.codegen.cli import main

ROOT = Path(__file__).resolve().parents[1]


def run() -> int:
    check = ["--check"] if "--check" in sys.argv else []
    applications = [
        (
            "examples.agent_runs:app",
            "tests/fixtures/agent_runs.openapi.json",
            "examples/agent_runs",
        ),
        (
            "tests.fixtures.models_app:app",
            "tests/fixtures/models.openapi.json",
            "tests/fixtures",
        ),
    ]
    result = 0
    for app, schema, output in applications:
        if not check:
            result |= main(["schema", "--app", app, "--output", str(ROOT / schema)])
        for language, directory in (
            ("typescript", "generated"),
            ("python", "generated_python"),
        ):
            policy = ["--strict"]
            if language == "typescript" and app == "examples.agent_runs:app":
                policy += [
                    "--allow-diagnostics",
                    str(ROOT / "tests/fixtures/agent_runs.typescript.diagnostics.json"),
                ]
            result |= main(
                [
                    "check" if check else "generate",
                    "--schema",
                    str(ROOT / schema),
                    "--language",
                    language,
                    "--output",
                    str(ROOT / output / directory),
                    *policy,
                    *(["--app", app] if check else []),
                ]
            )
    return result


if __name__ == "__main__":
    sys.exit(run())
