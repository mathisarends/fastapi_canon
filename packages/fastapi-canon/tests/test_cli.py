import subprocess
import sys
from pathlib import Path

import pytest

from fastapi_canon import __version__
from fastapi_canon.cli import main


@pytest.mark.parametrize("transport", ["sse", "websockets", "webrtc"])
def test_transport_help_uses_the_canon_command(
    transport: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([transport, "--help"])
    assert exit_info.value.code == 0
    assert f"canon {transport}" in capsys.readouterr().out


def test_version_is_shared_by_all_features() -> None:
    from fastapi_canon.integrations.sse import __version__ as sse_version
    from fastapi_canon.integrations.webrtc import __version__ as webrtc_version
    from fastapi_canon.integrations.websockets import __version__ as websockets_version

    assert sse_version == webrtc_version == websockets_version == __version__


def test_module_entry_point_checks_errors_without_importing_application(
    tmp_path: Path,
) -> None:
    (tmp_path / "api.py").write_text(
        "from fastapi_canon import CanonRouter\n"
        "raise RuntimeError('must not import')\n"
        "router = CanonRouter()\n"
        "@router.get('/')\n"
        "def endpoint() -> str: return 'ok'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", "-m", "fastapi_canon", "check", "errors", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Analyzed 1 operations" in result.stdout
