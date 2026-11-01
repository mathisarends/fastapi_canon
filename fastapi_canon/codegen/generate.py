from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

from fastapi_canon._version import __version__
from fastapi_canon.codegen.config import Configuration
from fastapi_canon.codegen.ir import GenerationError
from fastapi_canon.codegen.planner import build_plan

_MANIFEST = ".canon-codegen.json"


def generate(
    config: Configuration,
    *,
    targets: tuple[str, ...] = (),
    offline: bool = False,
    check: bool = False,
    diff: bool = False,
) -> tuple[bool, str]:
    from fastapi_canon.codegen.render import render

    selected = tuple(
        target for target in config.targets if not targets or target.name in targets
    )
    if not selected or set(targets) - {target.name for target in selected}:
        raise GenerationError("Unknown generation target")
    needed = {name for target in selected for name in target.sources}
    documents: dict[str, dict[str, Any]] = {}
    writes: dict[Path, str] = {}
    deletes: set[Path] = set()
    for source in config.sources:
        if source.name not in needed:
            continue
        if source.input is not None or offline:
            path = source.input or source.output
            assert path is not None
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GenerationError(f"Cannot read contract {path}: {exc}") from exc
        else:
            assert source.source is not None and source.output is not None
            document = _export(
                source.source, source.kind, source.factory, config.path.parent
            )
            writes[source.output] = _json(document)
        if not isinstance(document, dict):
            raise GenerationError(f"Source {source.name} must be a JSON object")
        documents[source.name] = document
    for target in selected:
        inputs = tuple(
            (source, documents[source.name])
            for source in config.sources
            if source.name in target.sources
        )
        files = render(build_plan(inputs), target)
        manifest = {
            "version": 1,
            "generator": {"name": "fastapi-canon", "version": __version__},
            "options": {
                "package": target.package,
                "clientName": target.client_name,
                "httpClient": target.http_client,
            },
            "mappings": {
                source.name: {
                    "operations": source.operations,
                    "identities": source.identities,
                    "names": source.names,
                    "includeOperations": source.include_operations,
                    "excludeOperations": source.exclude_operations,
                }
                for source, _ in inputs
            },
            "language": target.language,
            "sources": {
                name: hashlib.sha256(_json(documents[name]).encode()).hexdigest()
                for name in sorted(target.sources)
            },
            "files": sorted(files),
        }
        old: list[str] = []
        manifest_path = target.output / _MANIFEST
        if manifest_path.exists():
            try:
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                old = previous["files"]
                if (
                    previous["version"] != 1
                    or not isinstance(old, list)
                    or any(not isinstance(name, str) or not _safe(name) for name in old)
                ):
                    raise ValueError("invalid manifest")
            except (OSError, ValueError, KeyError) as exc:
                raise GenerationError(
                    f"Invalid ownership manifest at {manifest_path}"
                ) from exc
        for name, content in files.items():
            if not _safe(name):
                raise GenerationError(f"Unsafe generated path {name!r}")
            path = target.output / name
            if path.exists() and name not in old:
                raise GenerationError(f"Refusing to overwrite unowned file {path}")
            writes[path] = content
        writes[manifest_path] = _json(manifest)
        deletes.update(target.output / name for name in set(old) - files.keys())
    changed = []
    for path, content in writes.items():
        _no_symlink(path)
        if path.exists() and not path.is_file():
            raise GenerationError(f"Output is not a regular file: {path}")
        previous_text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not path.exists() or previous_text != content:
            changed.append((path, previous_text, content))
    for path in deletes:
        _no_symlink(path)
        if path.exists():
            changed.append((path, path.read_text(encoding="utf-8"), ""))
    report = "\n".join(str(path) for path, _, _ in changed)
    if diff:
        report = "".join(
            "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
            for path, before, after in changed
        )
    if not check:
        for path, _, content in changed:
            if path in deletes:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
    return bool(changed), report


def _export(reference: str, kind: str, factory: bool, root: Path) -> dict[str, Any]:
    module, sep, attribute = reference.partition(":")
    if not sep or not module or not attribute:
        raise GenerationError(
            f"Invalid application reference {reference!r}; use module:attribute"
        )
    sys.path.insert(0, str(root))
    try:
        value: Any = importlib.import_module(module)
        for part in attribute.split("."):
            value = getattr(value, part)
        if factory:
            value = value()
        if kind == "openapi":
            from fastapi_canon.integrations.sse.codegen.openapi import export_schema

            return export_schema(value)
        if kind == "openrpc":
            return cast(dict[str, Any], value.to_openrpc())
        from fastapi_canon.integrations.webrtc.contract import build_contract

        return build_contract(value)
    finally:
        sys.path.pop(0)


def _json(value: Any) -> str:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        + "\n"
    )


def _safe(name: str) -> bool:
    path = PurePosixPath(name)
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    return (
        not any(
            part.endswith((".", " ")) or part.split(".", 1)[0].upper() in reserved
            for part in path.parts
        )
        and bool(name)
        and bool(path.parts)
        and "\\" not in name
        and not path.is_absolute()
        and not any(part in {"..", "."} or ":" in part for part in path.parts)
        and name != _MANIFEST
    )


def _no_symlink(path: Path) -> None:
    if any(parent.exists() and not parent.is_dir() for parent in path.parents):
        raise GenerationError(f"Output parent is not a directory: {path}")
    if any(
        parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)
    ):
        raise GenerationError(
            f"Generated output cannot traverse a symbolic link or junction: {path}"
        )
