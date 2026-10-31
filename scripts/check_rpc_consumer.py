import argparse
import compileall
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi_canon.integrations.websockets.codegen import (
    PythonClientOptions,
    TypeScriptClientOptions,
    generate_python_client,
    generate_typescript_client,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check both SDKs for a consumer contract"
    )
    parser.add_argument("schema", type=Path)
    args = parser.parse_args()
    document = json.loads(args.schema.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="canon-rpc-consumer-") as temporary:
        working = Path(temporary)
        python = working / "consumer_client"
        typescript = working / "typescript"
        python_options = PythonClientOptions(
            package="consumer_client", with_transport="websocket"
        )
        typescript_options = TypeScriptClientOptions(with_transport="websocket")
        generate_python_client(document, python, python_options)
        generate_typescript_client(document, typescript, typescript_options)
        assert not generate_python_client(document, python, python_options, check=True)
        assert not generate_typescript_client(
            document, typescript, typescript_options, check=True
        )
        assert compileall.compile_dir(python, quiet=1)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import consumer_client",
                str(working),
            ],
            check=True,
            cwd=working,
        )
        ruff = shutil.which("ruff")
        assert ruff, "Install Ruff in the consumer-check environment"
        subprocess.run(
            [ruff, "check", str(python)],
            check=True,
            cwd=ROOT,
        )
        subprocess.run(
            [ruff, "format", "--check", str(python)],
            check=True,
            cwd=ROOT,
        )
        node = shutil.which("node")
        compiler = ROOT / "packages/fastapi-streams/node_modules/typescript/bin/tsc"
        assert node and compiler.is_file(), "Run npm ci for the Streams member first"
        subprocess.run(
            [
                node,
                str(compiler),
                "--strict",
                "--target",
                "ES2022",
                "--module",
                "ESNext",
                "--moduleResolution",
                "Bundler",
                "--lib",
                "ES2022,DOM,ESNext.Disposable",
                "--skipLibCheck",
                "--noEmit",
                "typescript/index.ts",
            ],
            cwd=working,
            check=True,
        )
    print(
        f"Both SDKs passed generation, drift and compilation: {args.schema} "
        f"({len(document['methods'])} methods)",
        flush=True,
    )


if __name__ == "__main__":
    main()
