import argparse
import ast
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from fastapi_canon.integrations.websockets.codegen import (
    render_python_client,
    render_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.bindings import load_binding
from fastapi_canon.integrations.websockets.codegen.ir import UnsupportedSchemaError
from fastapi_canon.integrations.websockets.codegen.options import (
    PythonClientOptions,
    TypeScriptClientOptions,
)
from fastapi_canon.integrations.websockets.codegen.writer import write_files
from fastapi_canon.integrations.websockets.schema.export import (
    ProtocolReferenceError,
    load_contract_source,
    render_contract,
)

LANGUAGES = ("python", "typescript")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "schema":
        return _schema(arguments)
    try:
        return _generate(arguments)
    except ModuleNotFoundError as error:
        if error.name != "jinja2":
            raise
        print(f"error: {error}", file=sys.stderr)
        return 2


def _schema(arguments: argparse.Namespace) -> int:
    """Render the contract of a protocol into the repository."""
    sys.path.insert(0, str(Path.cwd()))
    try:
        source = load_contract_source(arguments.source)
        contract = render_contract(
            source,
            title=arguments.title,
            description=arguments.description,
            servers=(
                tuple(_server(entry) for entry in arguments.server)
                if arguments.server
                else None
            ),
        )
    except ProtocolReferenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    output = arguments.output
    changed = write_files(output.parent, {output.name: contract}, check=arguments.check)
    if arguments.check:
        return _report_check(changed, subject="Contract")
    for path in changed:
        print(f"Wrote {path}")
    if not changed:
        print("Contract is up to date")
    return 0


def _generate(arguments: argparse.Namespace) -> int:
    if arguments.config is not None:
        return _generate_config(arguments.config, check=arguments.check)
    if (
        arguments.schema is None
        or arguments.language is None
        or arguments.output is None
    ):
        print(
            "error: schema, --language, and --output are required without --config",
            file=sys.stderr,
        )
        return 2
    return _generate_one(arguments)


def _generate_one(arguments: argparse.Namespace) -> int:
    document = json.loads(arguments.schema.read_text(encoding="utf-8"))
    try:
        files = _render_client(arguments, document)
    except (UnsupportedSchemaError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    changed = write_files(arguments.output, files, check=arguments.check)
    return _report_generation(changed, check=arguments.check)


def _render_client(
    arguments: argparse.Namespace,
    document: dict[str, object],
) -> dict[str, str]:
    bindings = tuple(load_binding(name) for name in getattr(arguments, "bindings", ()))
    if arguments.language == "python":
        extra_files, extra_exports = _extra_python_files(
            getattr(arguments, "extra_files", ()),
            getattr(arguments, "extra_exports", ()),
        )
        options = PythonClientOptions(
            package=arguments.package or arguments.output.name,
            client_name=arguments.client_name,
            api_root=arguments.api_root,
            api_names=dict(arguments.api_name),
            source=arguments.schema.name,
            with_transport=arguments.with_transport,
            extra_files=extra_files,
            extra_exports=extra_exports,
            bindings=bindings,
            contract=arguments.schema,
        )
        return dict(render_python_client(document, options))
    else:
        options = TypeScriptClientOptions(
            client_name=arguments.client_name,
            transport_module=arguments.transport_module,
            api_root=arguments.api_root,
            api_names=dict(arguments.api_name),
            source=arguments.schema.name,
            with_transport=arguments.with_transport,
            bindings=bindings,
            contract=arguments.schema,
        )
        return dict(render_typescript_client(document, options))


def _extra_python_files(
    paths: Sequence[Path], exports: Sequence[str]
) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    owners: dict[str, str] = {}
    for path in paths:
        if path.suffix != ".py" or path.name in files:
            raise ValueError(f"Invalid or duplicate extra Python file: {path}")
        content = path.read_text(encoding="utf-8")
        files[path.name] = content
        for node in ast.parse(content, filename=str(path)).body:
            names: list[str] = []
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [
                    target.id for target in node.targets if isinstance(target, ast.Name)
                ]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for name in names:
                if name in owners:
                    owners[name] = ""
                else:
                    owners[name] = path.stem
    resolved: dict[str, str] = {}
    for name in exports:
        if not name.isidentifier() or not owners.get(name):
            raise ValueError(
                f"Extra export {name!r} must have one definition in extra_files"
            )
        if name in resolved:
            raise ValueError(f"Duplicate extra export: {name}")
        resolved[name] = owners[name]
    return files, resolved


def _report_generation(changed: Sequence[Path], *, check: bool) -> int:
    if check:
        return _report_check(changed)
    for path in changed:
        print(f"Wrote {path}")
    if not changed:
        print("Client is up to date")
    return 0


def _generate_config(path: Path, *, check: bool) -> int:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        if config.get("version") != 1:
            raise ValueError("version must be 1")
        clients = config["clients"]
        if not isinstance(clients, list) or not clients:
            raise ValueError("clients must be a non-empty array of tables")
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"error: Invalid client config {path}: {error}", file=sys.stderr)
        return 2

    try:
        contract = config.get("contract")
        contract_job = (
            _config_contract(path.parent, contract) if contract is not None else None
        )
    except (KeyError, TypeError, ValueError, ProtocolReferenceError) as error:
        print(f"error: Invalid contract config: {error}", file=sys.stderr)
        return 2

    arguments_list: list[argparse.Namespace] = []
    for index, client in enumerate(clients, start=1):
        try:
            arguments_list.append(
                _config_arguments(
                    path.parent,
                    client,
                    check=check,
                    default_schema=(contract_job[0] if contract_job else None),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            print(f"error: Invalid clients[{index}]: {error}", file=sys.stderr)
            return 2

    contract_document = json.loads(contract_job[1]) if contract_job else None
    rendered: list[tuple[argparse.Namespace, dict[str, str]]] = []
    # Bindings named as module:attr resolve relative to the config file.
    sys.path.insert(0, str(path.parent.resolve()))
    try:
        for arguments in arguments_list:
            document = (
                contract_document
                if contract_job is not None and arguments.schema == contract_job[0]
                else json.loads(arguments.schema.read_text(encoding="utf-8"))
            )
            try:
                rendered.append((arguments, _render_client(arguments, document)))
            except (OSError, SyntaxError, UnsupportedSchemaError, ValueError) as error:
                print(f"error: Invalid client: {error}", file=sys.stderr)
                return 2
    finally:
        sys.path.pop(0)

    changed_contract: tuple[Path, ...] = ()
    if contract_job is not None:
        output, content = contract_job
        changed_contract = write_files(
            output.parent, {output.name: content}, check=check
        )
    failed = bool(changed_contract)
    if check:
        _report_check(changed_contract, subject="Contract")
    for arguments, files in rendered:
        changed = write_files(arguments.output, files, check=check)
        failed = bool(changed) or failed
        _report_generation(changed, check=check)
    return int(check and failed)


def _config_contract(base: Path, contract: object) -> tuple[Path, str]:
    if not isinstance(contract, dict):
        raise TypeError("contract must be a table")
    source_name = _config_string(contract, "source")
    output = base / _config_string(contract, "output")
    sys.path.insert(0, str(base))
    try:
        source = load_contract_source(source_name)
    finally:
        sys.path.pop(0)
    servers = contract.get("servers")
    if servers is not None:
        if not isinstance(servers, dict) or not all(
            isinstance(name, str) and isinstance(url, str)
            for name, url in servers.items()
        ):
            raise TypeError("contract.servers must be a string-to-string table")
        server_entries = tuple(
            {"name": name, "url": url} for name, url in servers.items()
        )
    else:
        server_entries = None
    content = render_contract(
        source,
        title=contract.get("title"),
        description=contract.get("description"),
        servers=server_entries,
    )
    return output, content


def _config_arguments(
    base: Path,
    client: object,
    *,
    check: bool,
    default_schema: Path | None = None,
) -> argparse.Namespace:
    if not isinstance(client, dict):
        raise TypeError("entry must be a table")
    if "schema" not in client and default_schema is None:
        raise KeyError("schema is required without a [contract] table")
    language = client["language"]
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES!r}")
    if language != "python" and (
        client.get("extra_files") or client.get("extra_exports")
    ):
        raise ValueError("extra_files and extra_exports require language='python'")
    api_names = client.get("api_names", {})
    if not isinstance(api_names, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in api_names.items()
    ):
        raise TypeError("api_names must be a string-to-string table")
    return argparse.Namespace(
        schema=(
            base / _config_string(client, "schema")
            if "schema" in client
            else default_schema
        ),
        language=language,
        output=base / _config_string(client, "output"),
        package=client.get("package"),
        client_name=client.get("client_name"),
        transport_module=client.get("transport_module", "../transport"),
        api_root=client.get("api_root"),
        api_name=list(api_names.items()),
        check=check,
        with_transport=client.get("with_transport"),
        extra_files=tuple(
            base / item for item in _config_string_list(client, "extra_files")
        ),
        extra_exports=_config_string_list(client, "extra_exports"),
        bindings=_config_string_list(client, "bindings"),
    )


def _config_string_list(client: dict[str, object], key: str) -> tuple[str, ...]:
    value = client.get(key, ())
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise TypeError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _config_string(client: dict[str, object], key: str) -> str:
    value = client[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _server(entry: str) -> dict[str, str]:
    name, separator, url = entry.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"Expected name=url, got {entry!r}")
    return {"name": name, "url": url}


def _report_check(changed: Sequence[Path], subject: str = "Client") -> int:
    if not changed:
        print(f"{subject} is up to date")
        return 0
    for path in changed:
        print(f"Out of date: {path}")
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canon websockets",
        description="Render RPC contracts and generate typed clients from them.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_schema_command(commands)
    _add_generate_command(commands)
    return parser


def _add_schema_command(commands: argparse._SubParsersAction) -> None:
    schema = commands.add_parser(
        "schema",
        help="Render the contract of a protocol.",
        description=(
            "Render an RpcChannel or RpcContract as the contract "
            "clients are generated from. The source is named as "
            "module:attribute and imported from the current directory."
        ),
    )
    schema.add_argument(
        "source", help="RpcChannel or RpcContract, as module:attribute."
    )
    schema.add_argument(
        "--output",
        type=Path,
        required=True,
        help="File the contract is written to.",
    )
    schema.add_argument(
        "--title",
        help="Title of the API (required unless the source is an RpcContract).",
    )
    schema.add_argument("--description", help="Description of the API.")
    schema.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="Server the API is reachable at; repeatable.",
    )
    schema.add_argument(
        "--check",
        action="store_true",
        help="Report an out-of-date contract instead of writing it.",
    )


def _add_generate_command(commands: argparse._SubParsersAction) -> None:
    generate = commands.add_parser("generate", help="Generate a client package.")
    generate.add_argument(
        "schema", nargs="?", type=Path, help="OpenRPC document to read."
    )
    generate.add_argument(
        "--language",
        choices=LANGUAGES,
        help="Target language for the generated client.",
    )
    generate.add_argument(
        "--output",
        type=Path,
        help="Directory of the generated package.",
    )
    generate.add_argument(
        "--config",
        type=Path,
        help="TOML manifest describing multiple client generation jobs.",
    )
    generate.add_argument(
        "--package",
        help="Import root of the generated package (default: the output directory).",
    )
    generate.add_argument(
        "--transport-module",
        default="../transport",
        help=('TypeScript module exporting RpcTransport (default: "../transport").'),
    )
    generate.add_argument(
        "--client-name",
        help="Name of the generated client class (default: derived from info.title).",
    )
    generate.add_argument(
        "--api-root",
        help="Explicit common wire prefix to omit from the public API tree.",
    )
    generate.add_argument(
        "--api-name",
        action="append",
        default=[],
        type=_name_mapping,
        metavar="PATH=NAME",
        help="Rename an API path or segment; repeatable.",
    )
    generate.add_argument(
        "--with-transport",
        choices=("websocket",),
        help="Generate a ready-to-use transport implementation.",
    )
    generate.add_argument(
        "--binding",
        action="append",
        default=[],
        dest="bindings",
        metavar="NAME",
        help=(
            "Typed client binding for a socket protocol: an entry point name "
            "such as 'cdp' or module:attribute; repeatable."
        ),
    )
    generate.add_argument(
        "--check",
        action="store_true",
        help="Report out-of-date files instead of writing them.",
    )


def _name_mapping(value: str) -> tuple[str, str]:
    path, separator, name = value.partition("=")
    if not separator or not path or not name:
        raise argparse.ArgumentTypeError(f"Expected path=name, got {value!r}")
    return path, name


if __name__ == "__main__":
    raise SystemExit(main())
