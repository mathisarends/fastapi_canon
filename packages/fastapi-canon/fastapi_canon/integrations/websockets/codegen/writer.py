import json
from collections.abc import Mapping
from pathlib import Path

MANIFEST = ".rpcgen/manifest.json"


def write_files(
    output_dir: Path,
    files: Mapping[str, str],
    *,
    check: bool = False,
) -> tuple[Path, ...]:
    """Write the rendered files and report which ones differed.

    With ``check`` nothing is written, so the returned paths are the files that
    are out of date.
    """
    previous = _generated_paths(output_dir)
    stale = previous - set(files)
    stale_paths = [
        (relative_path, _owned_path(output_dir, relative_path))
        for relative_path in sorted(stale)
    ]
    changed: list[Path] = []
    for relative_path, content in files.items():
        path = output_dir / relative_path
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        changed.append(path)
        if check:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    for _, path in stale_paths:
        if not path.is_file():
            continue
        changed.append(path)
        if check:
            continue
        path.unlink()
        _remove_empty_parents(path.parent, output_dir)
    return tuple(changed)


def _generated_paths(output_dir: Path) -> set[str]:
    manifest = output_dir / MANIFEST
    if not manifest.is_file():
        return set()
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        paths = document["files"]
        if not isinstance(paths, list) or not all(
            isinstance(path, str) for path in paths
        ):
            return set()
        return set(paths)
    except (json.JSONDecodeError, KeyError, OSError, TypeError):
        return set()


def _owned_path(output_dir: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe generated path in {MANIFEST}: {relative_path!r}")
    root = output_dir.resolve()
    path = (output_dir / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Unsafe generated path in {MANIFEST}: {relative_path!r}")
    return path


def _remove_empty_parents(directory: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    current = directory.resolve()
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
