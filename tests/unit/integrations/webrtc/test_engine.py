"""Public enum values at engine, event and signaling boundaries."""

import json
from dataclasses import asdict
from typing import Any

import pytest
from fastapi_canon.integrations.webrtc import (
    ConnectionState,
    ConnectionStats,
    DescriptionType,
    Direction,
    EngineCapabilities,
    MediaKind,
    MediaStats,
    MessageFormat,
    Role,
    SessionEvent,
    SessionEventKind,
)
from fastapi_canon.integrations.webrtc.engine import ChannelParameters
from fastapi_canon.integrations.webrtc.protocol import (
    DescriptionPayload,
    SessionMessage,
    encode,
)
from pydantic import TypeAdapter


def test_stats_and_events_serialize_with_existing_string_values() -> None:
    stats = ConnectionStats(
        connection_state=ConnectionState.CONNECTED,
        inbound=(MediaStats(kind=MediaKind.AUDIO, packets=7),),
    )
    event = SessionEvent(
        kind=SessionEventKind.TRACK,
        name="practice",
        session_id="session",
        elapsed_seconds=0,
        media_kind=MediaKind.AUDIO,
    )
    encoded = json.loads(json.dumps({"stats": asdict(stats), "event": asdict(event)}))
    assert encoded["stats"]["connection_state"] == "connected"
    assert encoded["stats"]["inbound"][0]["kind"] == "audio"
    assert encoded["event"]["kind"] == "track"
    assert encoded["event"]["media_kind"] == "audio"
    restored = TypeAdapter(ConnectionStats).validate_python(encoded["stats"])
    assert restored.connection_state is ConnectionState.CONNECTED
    assert restored.inbound[0].kind is MediaKind.AUDIO


def test_signaling_reuses_public_enums_without_changing_hello_values() -> None:
    payload = {
        "session_id": "session",
        "name": "practice",
        "role": "answerer",
        "ice_servers": [],
        "media": {"audio": "send_receive", "video": None, "max_incoming_tracks": 4},
        "channels": [
            {
                "name": "chat",
                "id": 0,
                "negotiated": True,
                "ordered": True,
                "max_retransmits": None,
                "max_packet_life_time_ms": None,
                "max_message_bytes": 1024,
                "receive_format": "json",
                "send_format": "text",
            }
        ],
        "limits": {
            "negotiation_timeout_ms": 1000,
            "max_signaling_message_bytes": 4096,
            "max_pending_candidates": 4,
        },
        "capabilities": {
            "trickle_ice": False,
            "ice_restart": False,
            "renegotiation": True,
        },
    }
    hello = SessionMessage.model_validate(payload)
    assert hello.role is Role.ANSWERER
    assert hello.media.audio is Direction.SEND_RECEIVE
    assert hello.channels[0].receive_format is MessageFormat.JSON
    assert hello.channels[0].send_format is MessageFormat.TEXT
    assert json.loads(encode(hello)) == {"type": "session", "protocol": 1, **payload}


def test_engine_flags_and_channel_options_require_keywords() -> None:
    capabilities: Any = EngineCapabilities
    parameters: Any = ChannelParameters
    with pytest.raises(TypeError):
        capabilities(False, False, True)
    with pytest.raises(TypeError):
        parameters("chat", False)
    assert EngineCapabilities(
        trickle_ice=False, ice_restart=False, renegotiation=True
    ).negotiated_data_channels
    assert not ChannelParameters("chat", ordered=False).ordered


@pytest.mark.parametrize("description_type", list(DescriptionType))
def test_description_type_round_trips_through_signaling(
    description_type: DescriptionType,
) -> None:
    payload = DescriptionPayload.model_validate(
        {"type": description_type.value, "sdp": "v=0\r\n"}
    )
    description = payload.to_engine()
    assert description.type is description_type
    assert DescriptionPayload.from_engine(description).model_dump_json() == (
        payload.model_dump_json()
    )
