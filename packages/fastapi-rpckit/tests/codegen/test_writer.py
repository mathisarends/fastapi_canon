import json
from pathlib import Path

import pytest
from fastapi_canon.integrations.websockets.codegen.writer import MANIFEST, write_files


def _manifest(*files: str) -> str:
    return json.dumps({"files": [*files, MANIFEST]}, indent=2) + "\n"


def test_a_new_run_removes_only_previously_generated_files(tmp_path: Path) -> None:
    output = tmp_path / "client"
    write_files(
        output,
        {
            "api/current.py": "current\n",
            "api/stale.py": "stale\n",
            MANIFEST: _manifest("api/current.py", "api/stale.py"),
        },
    )
    custom = output / "api" / "custom.py"
    custom.write_text("custom\n", encoding="utf-8")

    changed = write_files(
        output,
        {
            "api/current.py": "current\n",
            MANIFEST: _manifest("api/current.py"),
        },
    )

    assert changed == (output / MANIFEST, output / "api" / "stale.py")
    assert not (output / "api" / "stale.py").exists()
    assert custom.read_text(encoding="utf-8") == "custom\n"


def test_check_reports_stale_files_without_changing_anything(tmp_path: Path) -> None:
    output = tmp_path / "client"
    old_manifest = _manifest("stale.py")
    write_files(
        output,
        {"stale.py": "stale\n", MANIFEST: old_manifest},
    )

    changed = write_files(
        output,
        {MANIFEST: _manifest()},
        check=True,
    )

    assert changed == (output / MANIFEST, output / "stale.py")
    assert (output / "stale.py").read_text(encoding="utf-8") == "stale\n"
    assert (output / MANIFEST).read_text(encoding="utf-8") == old_manifest


def test_an_unsafe_manifest_path_is_rejected_before_writing(tmp_path: Path) -> None:
    output = tmp_path / "client"
    write_files(output, {MANIFEST: _manifest("../outside.py")})

    with pytest.raises(ValueError, match="Unsafe generated path"):
        write_files(output, {"new.py": "new\n", MANIFEST: _manifest("new.py")})

    assert not (output / "new.py").exists()
