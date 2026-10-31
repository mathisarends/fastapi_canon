import subprocess
import sys
from pathlib import Path

BLOCK_JINJA = """
import importlib.abc
import sys

class BlockJinja(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jinja2" or fullname.startswith("jinja2."):
            raise ModuleNotFoundError("blocked jinja2")
        return None

sys.meta_path.insert(0, BlockJinja())
"""


def run_without_jinja(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCK_JINJA + code],
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_cli_import_does_not_require_jinja() -> None:
    result = run_without_jinja(
        "import fastapi_canon.integrations.websockets\n"
        "import fastapi_canon.integrations.websockets.codegen\n"
        "import fastapi_canon.integrations.websockets.codegen.cli\n"
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_generate_without_jinja_reports_missing_extra(tmp_path: Path) -> None:
    schema = tmp_path / "contract.json"
    schema.write_text("{}", encoding="utf-8")
    result = run_without_jinja(
        "from fastapi_canon.integrations.websockets.codegen.cli import main\n"
        f"raise SystemExit(main(['generate', {str(schema)!r}, "
        f"'--language', 'python', '--output', {str(tmp_path / 'out')!r}]))\n"
    )

    assert result.returncode == 2
    assert result.stderr.strip() == (
        "error: Client generation requires the optional codegen dependencies; "
        "install fastapi-canon[codegen]"
    )
