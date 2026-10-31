"""Canon admission contracts through actual FastAPI WebSocket routing."""

from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from fastapi_canon import (
    Composition,
    Error,
    ErrorConfigurationError,
    ErrorOptions,
    ErrorRegistry,
    Feature,
    RouterPolicy,
)
from fastapi_canon.integrations.webrtc import CanonWebRtcRouter, Session
from fastapi_canon.integrations.webrtc.codegen.typescript import generate_typescript
from fastapi_canon.integrations.webrtc.contract import build_contract
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from starlette.testclient import WebSocketDenialResponse


class Missing(Exception):
    pass


class Forbidden(Exception):
    pass


MISSING = Error(Missing, status=404, code="browser_missing", title="Browser missing")
FORBIDDEN = Error(Forbidden, status=403, code="browser_forbidden", title="Forbidden")
ERRORS = ErrorRegistry(errors=[MISSING, FORBIDDEN])


class NoPeerEngine(AiortcEngine):
    async def create_peer(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("Admission denial must not allocate a peer")


def application(*, dependency: bool, declared: bool = True) -> FastAPI:
    router = CanonWebRtcRouter(engine=NoPeerEngine(), error_registry=ERRORS)

    def authorize() -> None:
        if dependency:
            raise Missing()

    @router.session("/{browser_id}", raises=[MISSING] if declared else [])
    async def video(
        browser_id: str, auth: Annotated[None, Depends(authorize)]
    ) -> Session:
        raise Missing()

    return Composition(
        Feature(name="browser", routers=[router]),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())


@pytest.mark.parametrize("dependency", [False, True])
@pytest.mark.parametrize("declared", [False, True])
def test_factory_and_dependency_problems_do_not_allocate_peers(
    dependency: bool, declared: bool
) -> None:
    app = application(dependency=dependency, declared=declared)
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDenialResponse) as caught,
        client.websocket_connect("/one"),
    ):
        pytest.fail("Denied session was accepted")
    assert caught.value.status_code == 404
    assert caught.value.headers["content-type"] == "application/problem+json"
    assert caught.value.json() == {
        "type": "https://example.test/problems/browser_missing",
        "title": "Browser missing",
        "status": 404,
        "code": "browser_missing",
    }
    metadata = build_contract(app)["sessions"][0]["admission_errors"]
    assert len(metadata) == int(declared)


def test_shared_local_contracts_use_composed_registry_and_effective_path() -> None:
    router = CanonWebRtcRouter(
        engine=NoPeerEngine(), error_registry=ERRORS, raises=[MISSING, MISSING]
    )

    @router.session("/{browser_id}", raises=[MISSING, FORBIDDEN, FORBIDDEN])
    async def video(browser_id: str) -> Session:
        return Session()

    feature = Feature(name="browser", routers=[router])
    assert feature.errors is not None
    assert feature.errors.errors == ERRORS.errors
    parent = APIRouter(prefix="/api")
    parent.include_router(router, prefix="/browser")
    nested = FastAPI()
    nested.include_router(parent)
    # A route-only export works when the local registry is fully resolved.
    with pytest.raises(ErrorConfigurationError, match="resolved problem type"):
        build_contract(nested.routes)
    app = Composition(
        Feature(name="browser", routers=[parent], errors=feature.errors),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())
    contract = build_contract(app)
    session = contract["sessions"][0]
    assert session["path"] == "/api/browser/{browser_id}"
    errors = session["admission_errors"]
    assert [error["code"] for error in errors] == [
        "browser_missing",
        "browser_forbidden",
    ]
    assert errors[0]["schema"]["properties"]["type"]["const"] == errors[0]["type"]
    generated = generate_typescript(contract)["client.ts"]
    assert "admissionErrors:" in generated
    assert 'code: "browser_missing"' in generated


def test_router_policy_applies_to_webrtc_admission_and_contract() -> None:
    def authorize() -> None:
        raise Forbidden()

    policy = RouterPolicy(
        error_registry=ErrorRegistry(errors=[FORBIDDEN]),
        dependencies=[Depends(authorize)],
        raises=[FORBIDDEN],
    )
    router = CanonWebRtcRouter(
        engine=NoPeerEngine(),
        policy=policy,
        error_registry=ErrorRegistry(errors=[MISSING]),
    )

    @router.session("/policy-video", raises=[MISSING])
    async def video() -> Session:
        return Session()

    app = Composition(
        Feature(name="video", routers=[router]),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())
    errors = build_contract(app)["sessions"][0]["admission_errors"]
    assert [error["code"] for error in errors] == [
        "browser_forbidden",
        "browser_missing",
    ]
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDenialResponse) as caught,
        client.websocket_connect("/policy-video"),
    ):
        pytest.fail("Denied session was accepted")
    assert caught.value.status_code == 403
    assert caught.value.json()["code"] == "browser_forbidden"


def test_invalid_declarations_fail_during_configuration() -> None:
    with pytest.raises(ErrorConfigurationError, match="requires an error registry"):
        CanonWebRtcRouter(engine=NoPeerEngine(), raises=[MISSING])
    router = CanonWebRtcRouter(
        engine=NoPeerEngine(), error_registry=ErrorRegistry(errors=[MISSING])
    )
    with pytest.raises(ErrorConfigurationError, match="does not belong"):
        router.session("/s", raises=[FORBIDDEN])
    with pytest.raises(ErrorConfigurationError, match="derives admission_errors"):
        router.session("/s", admission_errors=lambda app: [])


def test_error_after_accept_uses_protocol_close() -> None:
    async def ice(context: Any) -> Any:
        raise Missing()

    router = CanonWebRtcRouter(
        engine=NoPeerEngine(), error_registry=ERRORS, ice_servers=ice
    )

    @router.session("/s", raises=[MISSING])
    async def video() -> Session:
        return Session()

    app = Composition(
        Feature(name="browser", routers=[router]),
        errors=ErrorOptions(type_base="https://example.test/problems"),
    ).apply(FastAPI())
    with TestClient(app) as client, client.websocket_connect("/s") as socket:
        message = socket.receive()
        assert message == {
            "type": "websocket.close",
            "code": 1011,
            "reason": "handler_failure",
        }


def test_embedded_problem_schema_preserves_nested_extension_references() -> None:
    class Item(BaseModel):
        reason: str

    class Details(BaseModel):
        items: list[Item]

    error = Error(
        Missing,
        status=404,
        code="browser_missing",
        title="Missing",
        extensions_model=Details,
        extensions=lambda exc: Details(items=[Item(reason="gone")]),
    )
    registry = ErrorRegistry(errors=[error], type_base="https://example.test/problems")
    router = CanonWebRtcRouter(engine=NoPeerEngine(), error_registry=registry)

    @router.session("/s", raises=[error])
    async def video() -> Session:
        return Session()

    app = Composition(Feature(name="browser", routers=[router])).apply(FastAPI())
    contract = build_contract(app)
    schema = contract["sessions"][0]["admission_errors"][0]["schema"]
    with TestClient(app) as client:
        # Validate a real denial with the embedded schema, including local $defs.
        @app.get("/missing")
        async def missing() -> None:
            raise Missing()

        Draft202012Validator(schema).validate(client.get("/missing").json())
