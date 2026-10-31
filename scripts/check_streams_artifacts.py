import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/fastapi-streams"
CANON = ROOT / "packages/fastapi-canon"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check installed Streams CLI/backends")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((CANON / "pyproject.toml").read_text("utf-8"))["project"][
        "version"
    ]
    directory = ROOT / "dist/fastapi-canon"
    if args.artifacts and list(args.artifacts.glob(f"fastapi_canon-{version}-*.whl")):
        directory = args.artifacts.resolve()
    else:
        _run("uv", "build", "--package", "fastapi-canon", "--out-dir", str(directory))
    (wheel,) = directory.glob(f"fastapi_canon-{version}-*.whl")
    for backend in (None, "httpx", "httpx2"):
        with tempfile.TemporaryDirectory(prefix="canon-streams-") as temporary:
            environment = Path(temporary) / "venv"
            _run("uv", "venv", "--python", sys.executable, str(environment))
            python = environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            requirements = [str(wheel) + "[sse]"]
            flags = ["--core-only"]
            if backend:
                requirements = [str(wheel) + "[sse,codegen]", backend]
                flags = ["--http-client", backend]
            _run("uv", "pip", "install", "--python", str(python), *requirements)
            _run(
                str(python),
                "-I",
                str(PACKAGE / "scripts/smoke_package.py"),
                "--expected-version",
                version,
                *flags,
            )


def _run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
