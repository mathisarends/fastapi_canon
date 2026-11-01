"""Check the installed WebRTC CLI and type-check the client it generates.

The wheel is installed in a fresh environment. Its ``fastapi-canon webrtc`` command
exports the practice example's contract and generates its browser client; both
must match the committed files, and the generated TypeScript must compile.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = _ROOT / "examples/webrtc/practice"
_TSC = _ROOT / "node_modules/typescript/bin/tsc"
_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2022",
        "module": "NodeNext",
        "moduleResolution": "NodeNext",
        "strict": True,
        "exactOptionalPropertyTypes": True,
        "noUncheckedIndexedAccess": True,
        "noImplicitOverride": True,
        "forceConsistentCasingInFileNames": True,
        "lib": ["ES2022", "DOM", "DOM.Iterable"],
        "types": [],
        "noEmit": True,
    },
    "include": ["client/**/*.ts"],
}


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))["project"][
        "version"
    ]
    pattern = f"fastapi_canon-{version}-*.whl"
    directory = _ROOT / "dist/fastapi-canon"
    if args.artifacts and list(args.artifacts.glob(pattern)):
        directory = args.artifacts.resolve()
    else:
        _run("uv", "build", "--package", "fastapi-canon", "--out-dir", str(directory))
    (wheel,) = directory.glob(pattern)
    node = shutil.which("node")
    if node is None or not _TSC.is_file():
        raise SystemExit(
            "Install TypeScript first: pnpm install --frozen-lockfile --ignore-scripts"
        )
    with tempfile.TemporaryDirectory(prefix="canon-webrtc-") as temporary:
        working = Path(temporary)
        environment = working / "venv"
        _run("uv", "venv", "--python", sys.executable, str(environment))
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        cli = scripts / (
            "fastapi-canon.exe" if sys.platform == "win32" else "fastapi-canon"
        )
        _run("uv", "pip", "install", "--python", str(python), f"{wheel}[webrtc,aiortc]")
        # A copied module keeps the workspace source out of the import path.
        shutil.copyfile(_EXAMPLE / "app.py", working / "practice_app.py")
        shutil.copytree(_EXAMPLE / "client", working / "client")
        shutil.copyfile(_EXAMPLE / "contract.json", working / "contract.json")
        app = ("--app", "practice_app:app")
        _run(
            str(cli),
            "webrtc",
            "contract",
            *app,
            "--output",
            "contract.json",
            "--check",
            cwd=working,
        )
        _run(
            str(cli),
            "webrtc",
            "generate",
            "--contract",
            "contract.json",
            "--output",
            "client",
            "--client-name",
            "PracticeRtcClient",
            "--check",
            cwd=working,
        )
        (working / "package.json").write_text('{"type": "module"}\n', "utf-8")
        (working / "tsconfig.json").write_text(json.dumps(_TSCONFIG), "utf-8")
        _run(node, str(_TSC), "-p", str(working / "tsconfig.json"), cwd=working)
    print(f"Installed WebRTC CLI and generated client checks passed: {wheel.name}")


def _run(*command: str, cwd: Path = _ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    _main()
