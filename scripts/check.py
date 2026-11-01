import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE = _ROOT / "examples/reference-service"
_TRANSPORTS = ("sse", "websockets", "webrtc")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run Canon quality gates")
    parser.add_argument(
        "gate",
        choices=("all", "architecture", "lint", "types", "test", "clients", "browser"),
    )
    args = parser.parse_args()
    gates = (
        ("architecture", "lint", "types", "test", "clients")
        if args.gate == "all"
        else (args.gate,)
    )
    for gate in gates:
        if gate == "architecture":
            _run(
                _ROOT,
                "uv",
                "run",
                "--frozen",
                "smelt",
                "check",
                "--config",
                "smelt.yml",
                "--fail-on",
                "warning",
                python=False,
            )
        elif gate == "lint":
            _run(_ROOT, "scripts/check_documentation.py")
            # Moving configs can leave stale import classifications in Ruff's cache.
            _run(_ROOT, "-m", "ruff", "check", "--no-cache", ".")
            _run(_ROOT, "-m", "ruff", "format", "--no-cache", "--check", ".")
            for transport in _TRANSPORTS:
                source = f"fastapi_canon/integrations/{transport}"
                config = ("--config", f"tooling/{transport}.ruff.toml")
                _run(_ROOT, "-m", "ruff", "check", "--no-cache", *config, source)
                _run(
                    _ROOT,
                    "-m",
                    "ruff",
                    "format",
                    "--no-cache",
                    "--check",
                    *config,
                    source,
                )
        elif gate == "types":
            _run(_ROOT, "-m", "mypy")
            _run(
                _ROOT,
                "-m",
                "pyright",
                "--project",
                "tooling/sse.pyright.json",
                "--pythonpath",
                sys.executable,
            )
            _run(
                _ROOT,
                "-m",
                "pylint",
                "--rcfile",
                "tooling/sse.toml",
                "fastapi_canon/integrations/sse",
            )
            _run(_ROOT, "uv", "build", "--out-dir", "dist/typecheck", python=False)
            metadata = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))
            version = metadata["project"]["version"]
            (wheel,) = (_ROOT / "dist/typecheck").glob(f"fastapi_canon-{version}-*.whl")
            _run(
                _ROOT / "dist/typecheck",
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--no-cache",
                "--python",
                sys.executable,
                "--with",
                str(wheel) + "[codegen,websockets,otel]",
                "--with",
                "pyright==1.1.414",
                "python",
                str(_ROOT / "scripts/websockets/verify_types.py"),
                python=False,
            )
            _run(_REFERENCE, "-m", "mypy", "src", "tests")
            _run(_ROOT, "-m", "mypy", "--config-file", "tooling/webrtc.toml")
        elif gate == "test":
            _run(
                _ROOT,
                "-m",
                "pytest",
                "-q",
                "--cov=fastapi_canon.integrations.webrtc",
                "--cov-branch",
                "--cov-fail-under=85",
            )
            _run(_REFERENCE, "-m", "pytest", "-q")
        elif gate == "clients":
            for name in ("generated_clients", "socket_protocol", "typed_socket"):
                _run(
                    _ROOT,
                    "-m",
                    "fastapi_canon.integrations.websockets.codegen.cli",
                    "generate",
                    "--config",
                    f"examples/websockets/{name}/rpcgen.toml",
                    "--check",
                )
            _run(_ROOT, "scripts/sse/codegen.py", "--check")
            cli = ("-m", "fastapi_canon.integrations.webrtc.codegen.cli")
            contract = "examples/webrtc/practice/contract.json"
            _run(
                _ROOT,
                *cli,
                "contract",
                "--app",
                "examples.webrtc.practice.app:app",
                "--output",
                contract,
                "--check",
            )
            _run(
                _ROOT,
                *cli,
                "generate",
                "--contract",
                contract,
                "--client-name",
                "PracticeRtcClient",
                "--output",
                "examples/webrtc/practice/client",
                "--check",
            )
            for task in ("typecheck", "test:coverage"):
                _run(_ROOT, _pnpm(), "run", task, python=False)
        elif gate == "browser":
            environment = {**os.environ, "FASTAPI_WEBRTC_PYTHON": sys.executable}
            _run(_ROOT, _pnpm(), "run", "test:browser", python=False, env=environment)


def _pnpm() -> str:
    pnpm = shutil.which("pnpm.cmd") or shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit(
            "pnpm is required for TypeScript client checks; see docs/contributing.md"
        )
    return pnpm


def _run(
    root: Path, *arguments: str, python: bool = True, env: dict[str, str] | None = None
) -> None:
    command = [sys.executable, *arguments] if python else list(arguments)
    print(f"[{root.relative_to(_ROOT)}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=root, check=True, env=env)


if __name__ == "__main__":
    _main()
