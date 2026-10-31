"""Contract export from declared sessions."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi_canon.integrations.webrtc import Session, WebRtcRouter
from fastapi_canon.integrations.webrtc.contract import build_contract, dumps

from tests import contract_app
from tests.fake_engine import FakeEngine

PACKAGE = Path(__file__).parents[1]


def test_contract_lists_sessions_with_effective_paths() -> None:
    contract = build_contract(contract_app.app)
    assert contract["contract_version"] == 1
    assert contract["subprotocol"] == "fastapi-webrtc.v1"
    game, screen = contract["sessions"]
    assert game["path"] == "/api/rtc/games/{game_id}"
    assert game["description"].startswith("Play a game.")
    assert game["media"] == {
        "audio": "send_receive",
        "video": None,
        "max_incoming_tracks": 1,
    }
    assert screen == {
        "name": "screen_share",
        "path": "/api/rtc/screen",
        "role": "offerer",
        "description": None,
        "parameters": [],
        "media": {"audio": None, "video": None, "max_incoming_tracks": 4},
        "channels": [],
    }


def test_contract_parameters_keep_constraints_and_skip_headers() -> None:
    game = build_contract(contract_app.app)["sessions"][0]
    assert [(p["name"], p["in"], p["required"]) for p in game["parameters"]] == [
        ("game_id", "path", True),
        ("seat", "query", False),
        ("token", "query", True),
    ]
    seat = game["parameters"][1]["schema"]
    assert seat == {
        "type": "integer",
        "maximum": 3,
        "minimum": 0,
        "description": "Seat at the table.",
        "default": 0,
    }


def test_contract_channels_reference_shared_definitions() -> None:
    contract = build_contract(contract_app.app)
    moves, log, blob = contract["sessions"][0]["channels"]
    assert moves["ordered"] is False
    assert moves["max_retransmits"] == 0
    assert [item["$ref"] for item in moves["receive_schema"]["oneOf"]] == [
        "#/definitions/Move",
        "#/definitions/Chat",
    ]
    assert moves["send_schema"] == {"$ref": "#/definitions/State"}
    assert (log["id"], log["negotiated"], log["send_format"]) == (None, False, "text")
    assert (blob["receive_format"], blob["max_message_bytes"]) == ("binary", 1024)
    assert blob["receive_schema"] is None
    definitions = contract["definitions"]
    assert sorted(definitions) == ["Chat", "Move", "Point", "State"]
    # Fields with defaults are always present in server messages.
    assert definitions["State"]["required"] == ["turn", "pieces", "last"]
    assert definitions["Move"]["required"] == ["to"]


def test_contract_serialization_is_deterministic() -> None:
    first = dumps(build_contract(contract_app.app))
    assert first == dumps(build_contract(contract_app.app))
    assert first.endswith("}\n")
    assert json.loads(first) == build_contract(contract_app.app)


def test_contract_from_routes_and_empty_apps() -> None:
    assert build_contract(FastAPI())["sessions"] == []
    assert build_contract(FastAPI())["definitions"] == {}
    rtc = WebRtcRouter(engine=FakeEngine())

    @rtc.session("/s")
    def only() -> Session:
        return Session()

    assert [s["name"] for s in build_contract(rtc.routes)["sessions"]] == ["only"]


def test_duplicate_session_names_are_rejected() -> None:
    first = WebRtcRouter(engine=FakeEngine())
    second = WebRtcRouter(engine=FakeEngine())

    @first.session("/a", name="same")
    def a() -> Session:
        return Session()

    @second.session("/b", name="same")
    def b() -> Session:
        return Session()

    app = FastAPI()
    app.include_router(first)
    app.include_router(second)
    with pytest.raises(ValueError, match="more than once"):
        build_contract(app)
    with pytest.raises(ValueError, match="already declared"):
        first.session("/c", name="same")(a)


def test_router_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="max_sessions"):
        WebRtcRouter(engine=FakeEngine(), max_sessions=0)


def test_example_contract_is_current() -> None:
    from examples.practice.app import app

    committed = (PACKAGE / "examples/practice/contract.json").read_text(
        encoding="utf-8"
    )
    assert dumps(build_contract(app)) == committed


def test_admission_provider_receives_app_and_exports_detached_metadata() -> None:
    errors: list[dict[str, Any]] = [
        {"status": 403, "code": "denied", "schema": {"type": "object"}}
    ]
    seen: list[FastAPI | None] = []

    def admission(app: FastAPI | None) -> list[dict[str, Any]]:
        seen.append(app)
        return errors

    router = WebRtcRouter(engine=FakeEngine())

    @router.session("/s", admission_errors=admission)
    def session() -> Session:
        return Session()

    app = FastAPI()
    app.include_router(router)
    contract = build_contract(app)
    assert seen == [app]
    contract["sessions"][0]["admission_errors"][0]["schema"]["type"] = "string"
    assert errors[0]["schema"] == {"type": "object"}
    build_contract(router.routes)
    assert seen == [app, None]


def test_admission_provider_must_be_callable() -> None:
    router = WebRtcRouter(engine=FakeEngine())
    with pytest.raises(TypeError, match="contract provider"):
        router.session("/s", admission_errors=42)(lambda: Session())  # type: ignore[arg-type]
