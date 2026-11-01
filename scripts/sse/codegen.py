import sys
from pathlib import Path

from fastapi_canon.integrations.sse.codegen.cli import main

_ROOT = Path(__file__).resolve().parents[2]


def _run() -> int:
    check = ["--check"] if "--check" in sys.argv else []
    applications = [
        (
            "examples.sse.agent_runs:app",
            "tests/fixtures/sse/agent_runs.openapi.json",
            "examples/sse/agent_runs",
        ),
        (
            "tests.fixtures.sse.models_app:app",
            "tests/fixtures/sse/models.openapi.json",
            "tests/fixtures/sse",
        ),
    ]
    result = 0
    for app, schema, output in applications:
        if not check:
            result |= main(["schema", "--app", app, "--output", str(_ROOT / schema)])
        for language, directory in (
            ("typescript", "generated"),
            ("python", "generated_python"),
        ):
            policy = ["--strict"]
            if language == "typescript" and app == "examples.sse.agent_runs:app":
                policy += [
                    "--allow-diagnostics",
                    str(
                        _ROOT
                        / "tests/fixtures/sse/agent_runs.typescript.diagnostics.json"
                    ),
                ]
            result |= main(
                [
                    "check" if check else "generate",
                    "--schema",
                    str(_ROOT / schema),
                    "--language",
                    language,
                    "--output",
                    str(_ROOT / output / directory),
                    *policy,
                    *(["--app", app] if check else []),
                ]
            )
    return result


if __name__ == "__main__":
    sys.exit(_run())
