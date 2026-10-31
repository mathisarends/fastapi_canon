import ast

from fastapi_canon.integrations.sse.codegen.ir import build_ir
from fastapi_canon.integrations.sse.codegen.python import generate_python
from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript

from tests.codegen.helpers import event_document


def test_endpoint_prose_is_preserved_without_changing_generated_python_source():
    document = event_document(str)
    operation = document["paths"]["/events"]["get"]
    operation.update(
        summary="Follow events",
        description='Keeps \\paths and """quoted""" text.\nA */ comment terminator.',
        deprecated=True,
    )
    contract = build_ir(document)
    files = generate_python(contract)
    tree = ast.parse(files["streams/api.py"])
    api = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(
        node
        for node in api.body
        if isinstance(node, ast.FunctionDef) and node.name == "events"
    )
    doc = ast.get_docstring(method)
    assert doc is not None
    assert operation["summary"] in doc
    assert operation["description"] in doc
    assert "Deprecated endpoint." in doc
    assert "contextlib.aclosing" in doc
    assert "Events are validated with Pydantic against generated shapes" in doc
    assert "not full JSON Schema validation" in doc
    assert "unknown_events='ignore'" in doc
    assert "AbortSignal" not in doc
    assert "Opens the HTTP request when iteration begins." in doc


def test_generated_jsdoc_documents_runtime_limits_and_escapes_comment_endings():
    document = event_document(str)
    document["paths"]["/events"]["get"].update(
        description="Read events. */ This must remain documentation.", deprecated=True
    )
    files = generate_typescript(build_ir(document))
    api = files["streams/api.ts"]
    assert "Read events. *\\/ This must remain documentation." in api
    assert "@deprecated" in api
    assert "HTTP GET /events." in api
    assert "Does not automatically reconnect, retry, or replay events." in api
    assert "Caller cancellation propagates the AbortSignal reason." in api
    assert "are not validated at runtime." in api
    assert "contextlib.aclosing" not in api
