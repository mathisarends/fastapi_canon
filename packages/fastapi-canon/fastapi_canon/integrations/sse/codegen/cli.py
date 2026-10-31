from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import json
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI

from fastapi_canon.integrations.sse import __version__
from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    Document,
    export_schema,
)
from fastapi_canon.integrations.sse.codegen.writer import planned_files, write_files

if TYPE_CHECKING:
    from fastapi_canon.integrations.sse.codegen.diagnostics import GenerationDiagnostic

_CONFIG_KEYS = {"app", "schema", "clients"}
_CLIENT_KEYS = {
    "language",
    "output",
    "python-http-client",
    "strict",
    "allow-diagnostics",
    "diagnostics",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canon sse")
    commands = parser.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema", help="Export and validate OpenAPI 3.2")
    schema.add_argument("--app", required=True)
    schema.add_argument("--output", required=True, type=Path)
    schema.add_argument("--check", action="store_true")
    _add_diff(schema)
    for name in ("generate", "check"):
        command = commands.add_parser(
            name,
            help="Generate a client"
            if name == "generate"
            else "Check schema and generated-client drift",
        )
        command.add_argument("--schema", required=True, type=Path)
        command.add_argument(
            "--language", choices=["typescript", "python"], default="typescript"
        )
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--diagnostics", type=Path, help="Write JSON diagnostics")
        command.add_argument(
            "--strict", action="store_true", help="Fail on unaccepted diagnostics"
        )
        command.add_argument(
            "--allow-diagnostics",
            type=Path,
            help="JSON allowances with exact IDs and review reasons",
        )
        command.add_argument(
            "--python-http-client",
            choices=["httpx", "httpx2"],
            help="Python HTTP library (default: httpx; opt in with httpx2)",
        )
        if name == "generate":
            command.add_argument("--check", action="store_true")
        else:
            command.set_defaults(check=True)
            command.add_argument(
                "--app", help="Also compare the app's contract with --schema"
            )
        _add_diff(command)
    sync = commands.add_parser(
        "sync",
        help="Export the schema and generate every client configured in "
        "[tool.fastapi-streams]",
    )
    sync.add_argument(
        "--config",
        type=Path,
        default=Path("pyproject.toml"),
        help="TOML file with a [tool.fastapi-streams] table (default: pyproject.toml)",
    )
    sync.add_argument("--check", action="store_true")
    _add_diff(sync)
    args = parser.parse_args(argv)
    try:
        if args.diff and not args.check:
            raise ContractError("--diff requires --check")
        if args.command == "sync":
            return _sync(args.config, check=args.check, diff=args.diff)
        if args.command == "schema":
            content = _schema_text(_load_app(args.app))
            output = cast(Path, args.output)
            if args.check:
                if not output.is_file() or output.read_bytes() != content.encode(
                    "utf-8"
                ):
                    print(f"Out of date: {output}", file=sys.stderr)
                    if args.diff:
                        _print_diff(output, _read(output), content)
                    return 1
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(content, encoding="utf-8", newline="\n")
        else:
            if args.diagnostics:
                report = args.diagnostics.resolve()
                output = args.output.resolve()
                inputs = {args.schema.resolve()}
                if args.allow_diagnostics:
                    inputs.add(args.allow_diagnostics.resolve())
                if report in inputs or report == output or output in report.parents:
                    raise ContractError(
                        "--diagnostics must be outside the generated output "
                        "and input files"
                    )
            if args.language != "python" and args.python_http_client is not None:
                raise ContractError("--python-http-client requires --language python")
            from fastapi_canon.integrations.sse.codegen.diagnostics import (
                accepted_diagnostics,
                emit_diagnostics,
            )
            from fastapi_canon.integrations.sse.codegen.ir import build_ir
            from fastapi_canon.integrations.sse.codegen.python import generate_python
            from fastapi_canon.integrations.sse.codegen.typescript import (
                generate_typescript,
            )

            document = json.loads(args.schema.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ContractError("The OpenAPI document must be an object")
            document = cast(Document, document)
            generation = _generation_metadata(
                document, args.language, args.python_http_client or "httpx"
            )
            if args.command == "check" and args.app:
                expected = export_schema(_load_app(args.app))
                if expected != document:
                    print(
                        f"Out of date: {args.schema} (application contract changed)",
                        file=sys.stderr,
                    )
                    if args.diff:
                        _print_diff(
                            args.schema,
                            _schema_text_of(document),
                            _schema_text_of(expected),
                        )
                    return 1
            diagnostics: list[GenerationDiagnostic] = []
            contract = build_ir(document, target=args.language, diagnostics=diagnostics)
            # Reports precede strict-policy failures, so CI can review new IDs.
            if args.diagnostics:
                _write_diagnostics(
                    args.diagnostics,
                    diagnostics,
                    set(),
                    generation,
                )
            accepted: set[str] = (
                accepted_diagnostics(
                    diagnostics, json.loads(args.allow_diagnostics.read_text("utf-8"))
                )
                if args.allow_diagnostics
                else set()
            )
            if args.diagnostics:
                _write_diagnostics(
                    args.diagnostics,
                    diagnostics,
                    accepted,
                    generation,
                )
            pending = [item for item in diagnostics if item.id not in accepted]
            emit_diagnostics(pending)
            if args.strict and pending:
                raise ContractError(
                    f"{len(pending)} unaccepted generation diagnostics; "
                    "review --diagnostics and provide explicit --allow-diagnostics"
                )
            generated = (
                generate_python(
                    contract, http_client=args.python_http_client or "httpx"
                )
                if args.language == "python"
                else generate_typescript(contract)
            )
            changed = write_files(
                args.output, generated, check=args.check, generation=generation
            )
            if args.check and changed:
                for path in changed:
                    print(f"Out of date: {path}", file=sys.stderr)
                if args.diff:
                    planned = planned_files(generated, generation=generation)
                    root = args.output.resolve()
                    for path in changed:
                        name = path.relative_to(root).as_posix()
                        _print_diff(
                            args.output / name,
                            _read(path),
                            planned.get(name),
                        )
                return 1
    except (ContractError, OSError, ValueError, ImportError, AttributeError) as exc:
        message = str(exc)
        if isinstance(exc, ImportError) and exc.name in {"jinja2", "jsonschema"}:
            message = "Install fastapi-canon[codegen] to generate clients"
        print(f"canon sse: {message}", file=sys.stderr)
        return 2
    return 0


def _add_diff(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--diff",
        action="store_true",
        help="With --check, print a unified diff of out-of-date files to stdout",
    )


def _sync(config_path: Path, *, check: bool, diff: bool) -> int:
    """Run schema export and every configured client generation in order."""
    config = _load_config(config_path)
    root = config_path.resolve().parent
    flags = [*(["--check"] if check else []), *(["--diff"] if diff else [])]
    schema = str(root / config["schema"])
    steps: list[list[str]] = []
    if "app" in config:
        steps.append(["schema", "--app", config["app"], "--output", schema, *flags])
    for client in config["clients"]:
        step = [
            "generate",
            "--schema",
            schema,
            "--language",
            client["language"],
            "--output",
            str(root / client["output"]),
        ]
        if "python-http-client" in client:
            step += ["--python-http-client", client["python-http-client"]]
        if client.get("strict"):
            step.append("--strict")
        for option in ("allow-diagnostics", "diagnostics"):
            if option in client:
                step += [f"--{option}", str(root / client[option])]
        steps.append(step + flags)
    # Imports resolve against the configuration's directory, like a console script.
    sys.path.insert(0, str(root))
    status = 0
    for step in steps:
        if status == 1 and step[0] == "generate" and not Path(schema).is_file():
            # A missing schema was already reported; clients cannot be compared.
            break
        status = max(status, main(step))
        if status == 2:
            break
    return status


def _load_config(path: Path) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"Invalid TOML in {path}: {exc}") from exc
    tool: object = document.get("tool", {})
    config: object = (
        cast(dict[str, Any], tool).get("fastapi-streams")
        if isinstance(tool, dict)
        else None
    )
    if not isinstance(config, dict):
        raise ContractError(f"{path} has no [tool.fastapi-streams] table")
    config = cast(dict[str, Any], config)
    if unknown := set(config) - _CONFIG_KEYS:
        raise ContractError(
            f"Unknown [tool.fastapi-streams] keys: {', '.join(sorted(unknown))}"
        )
    if not isinstance(config.get("schema"), str):
        raise ContractError("[tool.fastapi-streams] requires schema = '<path>'")
    if "app" in config and not isinstance(config["app"], str):
        raise ContractError("[tool.fastapi-streams] app must be 'module:attribute'")
    clients = config.get("clients", [])
    if not isinstance(clients, list):
        raise ContractError("Use [[tool.fastapi-streams.clients]] tables for clients")
    for index, client in enumerate(cast(list[Any], clients)):
        where = f"tool.fastapi-streams.clients[{index}]"
        if not isinstance(client, dict):
            raise ContractError(f"{where} must be a table")
        client = cast(dict[str, Any], client)
        if unknown := set(client) - _CLIENT_KEYS:
            raise ContractError(f"Unknown {where} keys: {', '.join(sorted(unknown))}")
        if client.get("language") not in {"typescript", "python"}:
            raise ContractError(f"{where}.language must be 'typescript' or 'python'")
        for key in ("output", "python-http-client", "allow-diagnostics", "diagnostics"):
            if key in client and not isinstance(client[key], str):
                raise ContractError(f"{where}.{key} must be a string")
        if "output" not in client:
            raise ContractError(f"{where} requires output = '<directory>'")
        if not isinstance(client.get("strict", False), bool):
            raise ContractError(f"{where}.strict must be a boolean")
    if not clients and "app" not in config:
        raise ContractError("[tool.fastapi-streams] configures neither app nor clients")
    return {**config, "clients": clients}


def _schema_text(app: FastAPI) -> str:
    return _schema_text_of(export_schema(app))


def _schema_text_of(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="replace")


def _print_diff(path: Path, before: str | None, after: str | None) -> None:
    name = path.as_posix()
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        "/dev/null" if before is None else f"a/{name}",
        "/dev/null" if after is None else f"b/{name}",
    )
    for line in lines:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


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


def _generation_metadata(
    document: Document, target: str, http_client: str
) -> dict[str, str | None]:
    # Share canonical provenance between manifests and optional reports.
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "generator": "fastapi-canon",
        "generator_version": __version__,
        "schema_sha256": hashlib.sha256(encoded).hexdigest(),
        "schema_hash_encoding": "canonical-json-v1",
        "target": target,
        "python_http_client": http_client if target == "python" else None,
    }


def _write_diagnostics(
    path: Path,
    diagnostics: list[GenerationDiagnostic],
    accepted: set[str],
    generation: dict[str, str | None],
) -> None:
    python = generation["target"] == "python"
    request_phase = "method_call" if python else "not_performed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": generation,
                "validation": {
                    "full_json_schema": False,
                    "runtime_scope": (
                        "generated_types_and_supported_constraints"
                        if python
                        else "json_decoding_only"
                    ),
                    "parameters": {
                        "phase": request_phase,
                        "schema_validation": python,
                    },
                    "request_body": {
                        "phase": request_phase,
                        "schema_validation": python,
                    },
                    "events": {
                        "phase": "iteration",
                        "schema_validation": python,
                        "json_decoding": True,
                    },
                    "http_error_bodies": {
                        "phase": "consumer_controlled",
                        "schema_validation": False,
                    },
                },
                "diagnostics": [
                    item.to_dict(accepted=item.id in accepted) for item in diagnostics
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    sys.exit(main())
