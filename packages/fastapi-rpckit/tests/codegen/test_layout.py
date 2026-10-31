import posixpath
import re
from copy import deepcopy
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import (
    render_python_client,
    render_typescript_client,
)
from fastapi_canon.integrations.websockets.codegen.ir import UnsupportedSchemaError
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)


def test_nested_apis_share_top_level_namespace_modules(
    document: dict[str, Any],
) -> None:
    nested = deepcopy(document)
    first = nested["methods"][0]
    second = deepcopy(first)
    first["name"] = "tasks.list"
    second["name"] = "tasks.status.set"
    nested["methods"] = [first, second]
    nested["x-rpc-notifications"] = []

    python_files = render_python_client(
        nested,
        PythonClientOptions(package="task_client"),
    )
    typescript_files = render_typescript_client(
        nested,
        TypeScriptClientOptions(client_name="TaskClient"),
    )

    assert {name for name in python_files if name.startswith("namespaces/")} == {
        "namespaces/__init__.py",
        "namespaces/tasks.py",
    }
    assert "class TasksStatus:" in python_files["namespaces/tasks.py"]
    assert "class Tasks:" in python_files["namespaces/tasks.py"]
    assert "self.status = TasksStatus(rpc)" in python_files["namespaces/tasks.py"]
    assert "from .tasks import Tasks" in python_files["namespaces/__init__.py"]
    assert "from task_client.namespaces import Tasks" in python_files["client.py"]
    assert "task_client.namespaces.tasks.status" not in "".join(python_files.values())
    assert {name for name in typescript_files if name.startswith("namespaces/")} == {
        "namespaces/index.ts",
        "namespaces/tasks.ts",
    }
    assert "export class TasksStatus" in typescript_files["namespaces/tasks.ts"]
    assert "export class Tasks" in typescript_files["namespaces/tasks.ts"]
    assert (
        "this.status = new TasksStatus(rpc);" in typescript_files["namespaces/tasks.ts"]
    )
    assert 'from "../core"' in typescript_files["namespaces/tasks.ts"]
    assert 'from "./status"' not in typescript_files["namespaces/tasks.ts"]


def _without_named_errors(document: dict[str, Any]) -> dict[str, Any]:
    plain = deepcopy(document)
    for method in plain["methods"]:
        method.pop("errors", None)
    plain["servers"] = [
        {
            "name": "control",
            "url": "wss://api.example.com/rpc",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    return plain


def _python_modules(files: dict[str, str], package: str) -> set[str]:
    modules = set()
    for name in files:
        if not name.endswith(".py"):
            continue
        parts = name[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.add(".".join((package, *parts)) if parts else package)
    return modules


def _python_imports(content: str, package: str) -> set[str]:
    imported = set()
    for line in content.splitlines():
        if not line.startswith("from "):
            continue
        target = line.split()[1]
        if not target.startswith("."):
            imported.add(target)
            continue
        dots = len(target) - len(target.lstrip("."))
        parent = package.rsplit(".", dots - 1)[0] if dots > 1 else package
        imported.add(f"{parent}.{target[dots:]}".rstrip("."))
    return imported


def test_generated_python_modules_only_import_generated_modules(
    document: dict[str, Any],
) -> None:
    plain = _without_named_errors(document)

    files = render_python_client(
        plain,
        PythonClientOptions(package="plain_client", with_transport="websocket"),
    )

    modules = _python_modules(files, "plain_client")
    for name, content in files.items():
        if not name.endswith(".py"):
            continue
        package = ".".join(("plain_client", *name.split("/")[:-1]))
        for imported in _python_imports(content, package):
            if imported.startswith("plain_client"):
                assert imported in modules, f"{name} imports missing {imported}"


def test_generated_typescript_modules_only_import_generated_modules(
    document: dict[str, Any],
) -> None:
    plain = _without_named_errors(document)

    files = render_typescript_client(
        plain,
        TypeScriptClientOptions(client_name="PlainClient", with_transport="websocket"),
    )

    emitted = {name[: -len(".ts")] for name in files if name.endswith(".ts")}
    for name, content in files.items():
        if not name.endswith(".ts"):
            continue
        directory = name.rsplit("/", 1)[0] if "/" in name else ""
        for target in re.findall(r'from "(\.[^"]+)"', content):
            resolved = posixpath.normpath(posixpath.join(directory, target))
            assert resolved in emitted or f"{resolved}/index" in emitted, (
                f"{name} imports missing {target}"
            )


def test_a_stream_that_shadows_a_namespace_is_rejected(
    document: dict[str, Any],
) -> None:
    clashing = deepcopy(document)
    clashing["methods"][0]["name"] = "browser.screencast.start"
    clashing["methods"] = [clashing["methods"][0]]
    clashing["x-rpc-notifications"] = []
    clashing["x-rpckit-binary-streams"] = [
        {
            "name": "browser.screencast",
            "url": "wss://media/frames",
            "direction": "server-to-client",
            "contentType": "image/jpeg",
            "frameType": "binary",
        }
    ]

    for render, client_options in (
        (render_python_client, PythonClientOptions(package="browser_client")),
        (render_typescript_client, TypeScriptClientOptions(client_name="BrowserApi")),
    ):
        with pytest.raises(UnsupportedSchemaError) as error:
            render(clashing, client_options)

        assert "browser.screencast" in str(error.value)
