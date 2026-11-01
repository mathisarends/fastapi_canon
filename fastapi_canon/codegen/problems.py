from importlib.resources import files
from typing import Literal


def problem_runtime(language: Literal["python", "typescript"]) -> str:
    name = "problem.py" if language == "python" else "problem.ts"
    return (
        files("fastapi_canon.codegen")
        .joinpath("templates", name)
        .read_text(encoding="utf-8")
    )
