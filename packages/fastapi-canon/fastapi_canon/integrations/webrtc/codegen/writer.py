"""Deterministic generated-file output owned by a manifest."""

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

MANIFEST = ".fastapi-webrtc.json"


def write_files(
    output: Path,
    files: Mapping[str, str],
    *,
    check: bool = False,
    generation: Mapping[str, str | None] | None = None,
) -> tuple[Path, ...]:
    """Write deterministic output; only remove stale files owned by the manifest.

    Check mode is read-only, including when output does not yet exist.
    """
    expected = dict(files)
    if MANIFEST in expected:
        raise ValueError(f"{MANIFEST} is reserved")
    metadata: dict[str, Any] = {"files": sorted(files)}
    if generation is not None:
        metadata["generation"] = dict(generation)
    expected[MANIFEST] = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    manifest = _owned_path(output, MANIFEST)
    previous: set[str] = set()
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding="utf-8"))
        names = value.get("files")
        if not isinstance(names, list) or not all(
            isinstance(name, str) for name in names
        ):
            raise ValueError(f"Invalid generated manifest: {manifest}")
        previous = set(cast(list[str], names))
    # Preflight every target, including stale paths, before modifying anything.
    paths = {name: _owned_path(output, name) for name in set(expected) | previous}
    changed: list[Path] = []
    for name, content in sorted(expected.items()):
        path = paths[name]
        if path.is_file() and path.read_bytes() == content.encode("utf-8"):
            continue
        changed.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content.encode("utf-8"))
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    for name in sorted(previous - set(expected)):
        path = paths[name]
        if path.is_file():
            changed.append(path)
            if not check:
                path.unlink()
    return tuple(changed)


def _owned_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        raise ValueError(f"Unsafe generated path: {relative!r}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError(f"Generated path escapes output directory: {relative!r}")
    return resolved
