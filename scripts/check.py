import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMBERS = (
    ROOT / "packages/fastapi-canon",
    ROOT / "packages/fastapi-streams",
    ROOT / "packages/fastapi-rpckit",
    ROOT / "examples/reference-service",
    ROOT / "packages/fastapi-webrtc",
)
WEBRTC = MEMBERS[4]
SOURCES = {
    MEMBERS[1]: MEMBERS[0] / "fastapi_canon/integrations/sse",
    MEMBERS[2]: MEMBERS[0] / "fastapi_canon/integrations/websockets",
    WEBRTC: MEMBERS[0] / "fastapi_canon/integrations/webrtc",
}
WEBRTC_CLIENT = ("--output", "examples/practice/client", "--check")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Canon workspace quality gates")
    parser.add_argument(
        "gate", choices=("all", "lint", "types", "test", "clients", "browser")
    )
    args = parser.parse_args()
    gates = ("lint", "types", "test", "clients") if args.gate == "all" else (args.gate,)
    for gate in gates:
        if gate == "lint":
            _run(ROOT, "scripts/check_documentation.py")
            for root in (ROOT, *MEMBERS):
                targets = (
                    ("scripts", "tests", "examples/stack_service.py")
                    if root == ROOT
                    else (".",)
                )
                _run(root, "-m", "ruff", "check", *targets)
                _run(root, "-m", "ruff", "format", "--check", *targets)
                if root in SOURCES:
                    config = ("--config", str(root / "pyproject.toml"))
                    _run(root, "-m", "ruff", "check", *config, str(SOURCES[root]))
                    _run(
                        root,
                        "-m",
                        "ruff",
                        "format",
                        *config,
                        "--check",
                        str(SOURCES[root]),
                    )
        elif gate == "types":
            _run(MEMBERS[0], "-m", "mypy")
            _run(MEMBERS[1], "-m", "pyright", "--pythonpath", sys.executable)
            _run(MEMBERS[1], "-m", "pylint", str(SOURCES[MEMBERS[1]]))
            # Verify the installed public package; editable path discovery differs
            # between Pyright and Python, especially in a Windows workspace.
            _run(
                ROOT,
                "uv",
                "build",
                "--package",
                "fastapi-canon",
                "--out-dir",
                str(ROOT / "dist/typecheck"),
                python=False,
            )
            metadata = tomllib.loads((MEMBERS[0] / "pyproject.toml").read_text("utf-8"))
            version = metadata["project"]["version"]
            (wheel,) = (ROOT / "dist/typecheck").glob(f"fastapi_canon-{version}-*.whl")
            _run(
                MEMBERS[2],
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
                "scripts/verify_types.py",
                python=False,
            )
            _run(MEMBERS[3], "-m", "mypy", "src", "tests")
            _run(WEBRTC, "-m", "mypy")
        elif gate == "test":
            for root in (*MEMBERS, ROOT):
                coverage = ("--cov",) if root == WEBRTC else ()
                _run(root, "-m", "pytest", "-q", *coverage)
        elif gate == "clients":
            for config in (
                "examples/generated_clients/rpcgen.toml",
                "examples/socket_protocol/rpcgen.toml",
                "examples/typed_socket/rpcgen.toml",
            ):
                _run(
                    MEMBERS[2],
                    "-m",
                    "fastapi_canon.integrations.websockets.codegen.cli",
                    "generate",
                    "--config",
                    config,
                    "--check",
                )
            _run(MEMBERS[1], "scripts/codegen.py", "--check")
            cli = ("-m", "fastapi_canon.integrations.webrtc.codegen.cli")
            contract = "examples/practice/contract.json"
            _run(
                WEBRTC,
                *cli,
                "contract",
                "--app",
                "examples.practice.app:app",
                "--output",
                contract,
                "--check",
            )
            _run(
                WEBRTC,
                *cli,
                "generate",
                "--contract",
                contract,
                "--client-name",
                "PracticeRtcClient",
                *WEBRTC_CLIENT,
            )
            for member in (MEMBERS[1], WEBRTC):
                for task in ("typecheck", "test:coverage"):
                    _run(member, _npm(), "run", task, python=False)
        elif gate == "browser":
            # Real Chromium sessions against the aiortc engine.
            environment = {**os.environ, "FASTAPI_WEBRTC_PYTHON": sys.executable}
            _run(WEBRTC, _npm(), "run", "test:browser", python=False, env=environment)


def _npm() -> str:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise SystemExit("npm is required for TypeScript client checks")
    return npm


def _run(
    root: Path,
    *arguments: str,
    python: bool = True,
    env: dict[str, str] | None = None,
) -> None:
    command = [sys.executable, *arguments] if python else list(arguments)
    print(f"[{root.relative_to(ROOT) or '.'}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=root, check=True, env=env)


if __name__ == "__main__":
    main()
