import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi_canon.integrations.websockets import __version__
from fastapi_canon.integrations.websockets.codegen.ir import (
    ClientIr,
    UnsupportedSchemaError,
    build_ir,
)
from fastapi_canon.integrations.websockets.codegen.options import (
    PythonClientOptions,
    TypeScriptClientOptions,
)
from fastapi_canon.integrations.websockets.codegen.writer import MANIFEST, write_files

LAYOUT_VERSION = 10

__all__ = [
    "ClientIr",
    "PythonClientOptions",
    "TypeScriptClientOptions",
    "UnsupportedSchemaError",
    "build_ir",
    "generate_python_client",
    "generate_typescript_client",
    "render_python_client",
    "render_typescript_client",
    "write_files",
]


def render_python_client(
    document: dict[str, Any],
    options: PythonClientOptions,
) -> Mapping[str, str]:
    """Render a Python client package as relative path to file content."""
    from fastapi_canon.integrations.websockets.codegen.python import render_files

    files = render_files(build_ir(document), options)
    return _with_manifest(files, document, language="python")


def generate_python_client(
    document: dict[str, Any],
    output_dir: Path,
    options: PythonClientOptions,
    *,
    check: bool = False,
) -> tuple[Path, ...]:
    """Write a Python client package and report which files differed."""
    return write_files(output_dir, render_python_client(document, options), check=check)


def render_typescript_client(
    document: dict[str, Any],
    options: TypeScriptClientOptions,
) -> Mapping[str, str]:
    """Render a TypeScript client package as relative path to file content."""
    from fastapi_canon.integrations.websockets.codegen.typescript import (
        render_files as render_typescript_files,
    )

    files = render_typescript_files(build_ir(document), options)
    return _with_manifest(files, document, language="typescript")


def generate_typescript_client(
    document: dict[str, Any],
    output_dir: Path,
    options: TypeScriptClientOptions,
    *,
    check: bool = False,
) -> tuple[Path, ...]:
    """Write a TypeScript client package and report which files differed."""
    return write_files(
        output_dir,
        render_typescript_client(document, options),
        check=check,
    )


def _with_manifest(
    files: Mapping[str, str],
    document: dict[str, Any],
    *,
    language: str,
) -> dict[str, str]:
    generated = dict(files)
    generated[MANIFEST] = (
        json.dumps(
            {
                "generator": "fastapi-canon",
                "generatorVersion": __version__,
                "layoutVersion": LAYOUT_VERSION,
                "language": language,
                "contractSha256": hashlib.sha256(
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "files": sorted((*generated, MANIFEST)),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return generated
