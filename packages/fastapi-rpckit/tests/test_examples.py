import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = tuple(sorted((Path(__file__).parents[1] / "examples").glob("*.py")))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_example_runs(example: Path) -> None:
    subprocess.run(
        [sys.executable, str(example)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_generated_client_examples_match_their_source() -> None:
    config = Path(__file__).parents[1] / "examples/generated_clients/rpcgen.toml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "fastapi_canon.integrations.websockets.codegen.cli",
            "generate",
            "--config",
            str(config),
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
