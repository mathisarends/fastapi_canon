"""``canon webrtc`` command line: export contracts and generate clients."""

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from fastapi_canon.integrations.webrtc import __version__
from fastapi_canon.integrations.webrtc.codegen.typescript import (
    DEFAULT_CLIENT_NAME,
    ContractError,
    generate_typescript,
)
from fastapi_canon.integrations.webrtc.codegen.writer import write_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canon webrtc")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    contract = commands.add_parser(
        "contract", help="Export the session contract of a FastAPI application"
    )
    contract.add_argument("--app", required=True, help="module:attribute")
    contract.add_argument("--output", required=True, type=Path)
    contract.add_argument(
        "--check", action="store_true", help="Fail if the file is out of date"
    )

    generate = commands.add_parser(
        "generate", help="Generate the TypeScript browser client of a contract"
    )
    source = generate.add_mutually_exclusive_group(required=True)
    source.add_argument("--contract", type=Path, help="Contract JSON file")
    source.add_argument("--app", help="module:attribute, instead of --contract")
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument("--client-name", default=DEFAULT_CLIENT_NAME)
    generate.add_argument(
        "--check", action="store_true", help="Fail if generated files are out of date"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "contract":
            return _contract(args.app, cast(Path, args.output), check=args.check)
        return _generate(args)
    except (ContractError, OSError, ValueError, ImportError, AttributeError) as exc:
        print(f"canon webrtc: {exc}", file=sys.stderr)
        return 2


def _contract(app: str, output: Path, *, check: bool) -> int:
    from fastapi_canon.integrations.webrtc.contract import build_contract, dumps

    content = dumps(build_contract(_load_app(app)))
    if check:
        if not output.is_file() or output.read_bytes() != content.encode("utf-8"):
            print(f"Out of date: {output}", file=sys.stderr)
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="\n")
    return 0


def _generate(args: argparse.Namespace) -> int:
    contract: Any
    if args.app is not None:
        from fastapi_canon.integrations.webrtc.contract import build_contract

        contract = build_contract(_load_app(args.app))
    else:
        contract = json.loads(cast(Path, args.contract).read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ContractError("The contract must be a JSON object")
    encoded = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    generation: dict[str, str | None] = {
        "generator": "fastapi-canon",
        "generator_version": __version__,
        "contract_sha256": hashlib.sha256(encoded).hexdigest(),
        "contract_hash_encoding": "canonical-json-v1",
        "target": "typescript",
        "client_name": args.client_name,
    }
    files = generate_typescript(contract, client_name=args.client_name)
    changed = write_files(
        cast(Path, args.output), files, check=args.check, generation=generation
    )
    if args.check and changed:
        for path in changed:
            print(f"Out of date: {path}", file=sys.stderr)
        return 1
    return 0


def _load_app(value: str) -> FastAPI:
    module, separator, attribute = value.partition(":")
    if not separator or not module or not attribute:
        raise ContractError("--app must use module:attribute syntax")
    # Console scripts start with their installation directory on sys.path.
    sys.path.insert(0, str(Path.cwd()))
    app = getattr(importlib.import_module(module), attribute)
    if not isinstance(app, FastAPI):
        raise ContractError(f"{value} is not a FastAPI application")
    return app


if __name__ == "__main__":
    sys.exit(main())
