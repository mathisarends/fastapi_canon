import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.schema import render_openrpc

from tests.conftest import greeting_app

PACKAGE = "greeting_client"


@pytest.fixture(scope="session")
def document() -> dict[str, Any]:
    return render_openrpc(greeting_app.protocol, title="Greeting")


@pytest.fixture(scope="session")
def options() -> PythonClientOptions:
    return PythonClientOptions(
        package=PACKAGE,
        client_name="GreetingClient",
        source="greeting.openrpc.json",
    )


@pytest.fixture(scope="session")
def generated_client(
    document: dict[str, Any],
    options: PythonClientOptions,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ModuleType]:
    root = tmp_path_factory.mktemp("generated")
    generate_python_client(document, root / PACKAGE, options)
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    try:
        yield importlib.import_module(PACKAGE)
    finally:
        sys.path.remove(str(root))
        for name in [
            name
            for name in sys.modules
            if name == PACKAGE or name.startswith(f"{PACKAGE}.")
        ]:
            del sys.modules[name]
