"""Check the installed WebRTC CLI and type-check the client it generates.

The wheel is installed in a fresh environment. Its ``canon webrtc`` command
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

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/fastapi-webrtc"
CANON = ROOT / "packages/fastapi-canon"
EXAMPLE = PACKAGE / "examples/practice"
TSC = PACKAGE / "node_modules/typescript/bin/tsc"
TSCONFIG = {
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((CANON / "pyproject.toml").read_text("utf-8"))["project"][
        "version"
    ]
    pattern = f"fastapi_canon-{version}-*.whl"
    directory = ROOT / "dist/fastapi-canon"
    if args.artifacts and list(args.artifacts.glob(pattern)):
        directory = args.artifacts.resolve()
    else:
        _run("uv", "build", "--package", "fastapi-canon", "--out-dir", str(directory))
    (wheel,) = directory.glob(pattern)
    node = shutil.which("node")
    if node is None or not TSC.is_file():
        raise SystemExit(
            "Install TypeScript first: "
            "npm ci --ignore-scripts --prefix packages/fastapi-webrtc"
        )
    with tempfile.TemporaryDirectory(prefix="canon-webrtc-") as temporary:
        working = Path(temporary)
        environment = working / "venv"
        _run("uv", "venv", "--python", sys.executable, str(environment))
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        cli = scripts / ("canon.exe" if sys.platform == "win32" else "canon")
        _run("uv", "pip", "install", "--python", str(python), f"{wheel}[webrtc,aiortc]")
        # A copied module keeps the workspace source out of the import path.
        shutil.copyfile(EXAMPLE / "app.py", working / "practice_app.py")
        shutil.copytree(EXAMPLE / "client", working / "client")
        shutil.copyfile(EXAMPLE / "contract.json", working / "contract.json")
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
        (working / "tsconfig.json").write_text(json.dumps(TSCONFIG), "utf-8")
        _run(node, str(TSC), "-p", str(working / "tsconfig.json"), cwd=working)
    print(f"Installed WebRTC CLI and generated client checks passed: {wheel.name}")


def _run(*command: str, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    main()
