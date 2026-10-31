import json
import re
import subprocess
import sys
from pathlib import Path


def test_python_client_rejects_invalid_calls_and_preserves_narrowing(tmp_path):
    root = Path(__file__).parents[2]
    fixture = root / "tests/typing/client_usage.py"
    expected = {
        (line, match[1])
        for line, text in enumerate(fixture.read_text().splitlines())
        if (match := re.search(r"expect-error: (\w+)", text))
    }
    (tmp_path / "client_usage.py").write_text(fixture.read_text(), encoding="utf-8")
    config = tmp_path / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": ["client_usage.py"],
                "extraPaths": [str(root)],
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
            str(config),
            "--pythonpath",
            sys.executable,
            "--outputjson",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    errors = report["generalDiagnostics"]
    actual = {(error["range"]["start"]["line"], error.get("rule")) for error in errors}
    assert expected <= actual, report
    # Pyright may report unknown types downstream of an intentional bad access.
    assert all(line in {item[0] for item in expected} for line, _ in actual), report
