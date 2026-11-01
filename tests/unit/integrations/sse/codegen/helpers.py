import importlib.util
import sys
from collections.abc import AsyncIterator
from contextlib import contextmanager
from uuid import uuid4

from fastapi import FastAPI
from fastapi_canon.integrations.sse import StreamRouter
from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.openapi import export_schema
from fastapi_canon.integrations.sse.codegen.writer import write_files


def event_contract(event_type):
    return build_ir(event_document(event_type))


def event_document(event_type):
    router = StreamRouter()

    async def events():
        yield None

    events.__annotations__["return"] = AsyncIterator[event_type]
    router.stream("/events", operation_id="events")(events)
    app = FastAPI()
    app.include_router(router)
    return export_schema(app)


def schema_contract(schema, components=None):
    document = event_document(str)
    media = document["paths"]["/events"]["get"]["responses"]["200"]["content"]
    media["text/event-stream"]["itemSchema"]["properties"]["data"]["contentSchema"] = (
        schema
    )
    document["components"] = {"schemas": components or {}}
    return build_ir(document)


@contextmanager
def load_models(tmp_path, files):
    path = tmp_path / "models.py"
    path.write_text(files["models.py"], encoding="utf-8")
    name = "generated_models_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@contextmanager
def load_package(tmp_path, files):
    write_files(tmp_path, files)
    name = "generated_sdk_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, tmp_path / "__init__.py", submodule_search_locations=[str(tmp_path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key)
