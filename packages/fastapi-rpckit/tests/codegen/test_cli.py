import json
from pathlib import Path
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen.cli import main

from .conftest import PACKAGE


@pytest.fixture
def schema(document: dict[str, Any], tmp_path: Path) -> Path:
    path = tmp_path / "greeting.openrpc.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_generate_writes_the_package(schema: Path, tmp_path: Path) -> None:
    output = tmp_path / PACKAGE

    assert (
        main(
            [
                "generate",
                str(schema),
                "--language",
                "python",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert (output / "client.py").exists()
    assert (output / "namespaces" / "greeting.py").exists()


def test_the_package_name_defaults_to_the_output_directory(
    schema: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / PACKAGE
    main(
        [
            "generate",
            str(schema),
            "--language",
            "python",
            "--output",
            str(output),
        ]
    )

    assert f"from {PACKAGE}.models import" in (
        output / "namespaces" / "greeting.py"
    ).read_text(encoding="utf-8")


def test_the_client_class_can_be_named(schema: Path, tmp_path: Path) -> None:
    output = tmp_path / PACKAGE
    main(
        [
            "generate",
            str(schema),
            "--language",
            "python",
            "--output",
            str(output),
            "--client-name",
            "GreetingClient",
        ]
    )

    assert "class GreetingClient:" in (output / "client.py").read_text(encoding="utf-8")


def test_check_fails_when_the_client_is_missing(schema: Path, tmp_path: Path) -> None:
    output = tmp_path / PACKAGE

    assert (
        main(
            [
                "generate",
                str(schema),
                "--language",
                "python",
                "--output",
                str(output),
                "--check",
            ]
        )
        == 1
    )
    assert not output.exists()


def test_check_passes_once_the_client_is_generated(
    schema: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / PACKAGE
    main(
        [
            "generate",
            str(schema),
            "--language",
            "python",
            "--output",
            str(output),
        ]
    )

    assert (
        main(
            [
                "generate",
                str(schema),
                "--language",
                "python",
                "--output",
                str(output),
                "--check",
            ]
        )
        == 0
    )


def test_generate_writes_a_typescript_client(schema: Path, tmp_path: Path) -> None:
    output = tmp_path / "generated"

    assert (
        main(
            [
                "generate",
                str(schema),
                "--language",
                "typescript",
                "--output",
                str(output),
                "--client-name",
                "GreetingClient",
                "--transport-module",
                "../rpc-transport",
            ]
        )
        == 0
    )
    assert "export class GreetingClient" in (output / "client.ts").read_text(
        encoding="utf-8"
    )
    assert 'from "../rpc-transport"' in (output / "core.ts").read_text(encoding="utf-8")


def test_generate_config_builds_independent_sibling_clients(
    schema: Path,
    tmp_path: Path,
) -> None:
    config = tmp_path / "rpc-clients.toml"
    config.write_text(
        "version = 1\n\n"
        "[[clients]]\n"
        'schema = "greeting.openrpc.json"\n'
        'language = "python"\n'
        'output = "generated/greeting_python"\n'
        'package = "generated.greeting_python"\n'
        'client_name = "GreetingClient"\n\n'
        "[[clients]]\n"
        'schema = "greeting.openrpc.json"\n'
        'language = "typescript"\n'
        'output = "generated/greeting-typescript"\n'
        'client_name = "GreetingClient"\n'
        'transport_module = "../../transport"\n',
        encoding="utf-8",
    )

    assert main(["generate", "--config", str(config)]) == 0
    assert (tmp_path / "generated" / "greeting_python" / "client.py").exists()
    assert (tmp_path / "generated" / "greeting-typescript" / "client.ts").exists()
    assert main(["generate", "--config", str(config), "--check"]) == 0


def test_config_includes_extra_python_file_and_export(
    schema: Path, tmp_path: Path
) -> None:
    extra = tmp_path / "src" / "extra" / "authenticated.py"
    extra.parent.mkdir(parents=True)
    extra.write_text(
        "def connect_gateway():\n    return 'connected'\n", encoding="utf-8"
    )
    config = tmp_path / "rpcgen.toml"
    config.write_text(
        "version = 1\n\n[[clients]]\n"
        'schema = "greeting.openrpc.json"\n'
        'language = "python"\n'
        'output = "greeting_client"\n'
        'extra_files = ["src/extra/authenticated.py"]\n'
        'extra_exports = ["connect_gateway"]\n',
        encoding="utf-8",
    )
    assert main(["generate", "--config", str(config)]) == 0
    output = tmp_path / "greeting_client"
    assert (output / "authenticated.py").read_text(encoding="utf-8") == (
        extra.read_text(encoding="utf-8")
    )
    assert "from .authenticated import connect_gateway" in (
        output / "__init__.py"
    ).read_text(encoding="utf-8")
    manifest = json.loads(
        (output / ".rpcgen/manifest.json").read_text(encoding="utf-8")
    )
    assert "authenticated.py" in manifest["files"]
    assert main(["generate", "--config", str(config), "--check"]) == 0


def test_generate_config_renders_contract_and_clients_together(tmp_path: Path) -> None:
    config = tmp_path / "rpcgen.toml"
    config.write_text(
        "version = 1\n\n"
        "[contract]\n"
        'source = "tests.test_contract:contract"\n'
        'output = "schemas/health.openrpc.json"\n\n'
        "[[clients]]\n"
        'language = "typescript"\n'
        'output = "generated/health"\n'
        'client_name = "HealthClient"\n'
        'with_transport = "websocket"\n',
        encoding="utf-8",
    )

    assert main(["generate", "--config", str(config)]) == 0
    schema = tmp_path / "schemas" / "health.openrpc.json"
    assert json.loads(schema.read_text(encoding="utf-8"))["info"]["title"] == (
        "Control API"
    )
    assert (tmp_path / "generated" / "health" / "client.ts").exists()
    assert main(["generate", "--config", str(config), "--check"]) == 0

    schema.write_text("{}\n", encoding="utf-8")
    assert main(["generate", "--config", str(config), "--check"]) == 1


def test_generate_passes_api_tree_options_to_both_emitters(
    schema: Path,
    tmp_path: Path,
) -> None:
    document = json.loads(schema.read_text(encoding="utf-8"))
    document["methods"][0]["name"] = "browser.nav.navigate"
    document["methods"] = [document["methods"][0]]
    schema.write_text(json.dumps(document), encoding="utf-8")

    for language, leaf in (
        ("python", "navigation.py"),
        ("typescript", "navigation.ts"),
    ):
        output = tmp_path / language
        assert (
            main(
                [
                    "generate",
                    str(schema),
                    "--language",
                    language,
                    "--output",
                    str(output),
                    "--api-root",
                    "browser",
                    "--api-name",
                    "nav=navigation",
                ]
            )
            == 0
        )
        assert (output / "namespaces" / leaf).exists()
