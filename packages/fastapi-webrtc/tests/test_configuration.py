"""Declarations, limits, ICE helpers, SDP inspection and error categories."""

import base64
import hashlib
import hmac
from typing import Any, Never, NoReturn, assert_type

import pytest
from fastapi_canon.integrations.webrtc import (
    ConnectionState,
    DataChannelSpec,
    Direction,
    ErrorCode,
    IceServer,
    IceServers,
    MediaKind,
    MediaPolicy,
    Peer,
    Role,
    Session,
    SessionClosedError,
    SessionDeclaration,
    SessionLimits,
    TurnRestCredentials,
    WebRtcError,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.declarations import SessionContext
from fastapi_canon.integrations.webrtc.engine import (
    CandidatePairStats,
    ConnectionStats,
    MediaStats,
)
from fastapi_canon.integrations.webrtc.errors import close_code, default_message
from fastapi_canon.integrations.webrtc.ice import resolve_ice_servers
from fastapi_canon.integrations.webrtc.sdp import summarize
from pydantic import BaseModel

from tests.fake_engine import FakeEngine


def declaration(
    *channels: DataChannelSpec, role: Role = Role.ANSWERER
) -> SessionDeclaration:
    return SessionDeclaration(
        name="s",
        path="/s",
        role=role,
        media=MediaPolicy(),
        channels=channels,
        limits=SessionLimits(),
    )


def test_directions() -> None:
    assert Direction.SEND.sends and not Direction.SEND.receives
    assert Direction.RECEIVE.receives and not Direction.RECEIVE.sends
    assert Direction.SEND_RECEIVE.sends and Direction.SEND_RECEIVE.receives
    policy = MediaPolicy(audio=Direction.SEND)
    assert policy.direction("audio") is Direction.SEND
    assert policy.direction("video") is None
    assert policy.direction("application") is None
    with pytest.raises(ValueError, match="negative"):
        MediaPolicy(max_incoming_tracks=-1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "bad name"}, "identifier"),
        ({"name": "x" * 64}, "identifier"),
        (
            {"name": "c", "max_retransmits": 1, "max_packet_life_time_ms": 5},
            "exclusive",
        ),
        ({"name": "c", "max_message_bytes": 0}, "positive"),
        ({"name": "c", "id": 65535}, "between"),
    ],
)
def test_invalid_channels(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DataChannelSpec(**kwargs)  # type: ignore[call-overload]


def test_channel_formats_and_adapters() -> None:
    raw = DataChannelSpec(name="raw")
    text = DataChannelSpec(name="text", receive_model=str, send_model=str)
    binary = DataChannelSpec(name="blob", receive_model=bytes, send_model=bytes)
    typed = DataChannelSpec(name="typed", receive_model=int, send_model=list[int])
    assert (raw.receive_format, raw.send_format) == ("raw", "raw")
    assert (text.receive_format, text.send_format) == ("text", "text")
    assert (binary.receive_format, binary.send_format) == ("binary", "binary")
    assert (typed.receive_format, typed.send_format) == ("json", "json")
    assert raw.receive_adapter is None and text.send_adapter is None
    assert typed.receive_adapter is not None
    assert typed.receive_adapter.validate_json("3") == 3
    one_way = DataChannelSpec(name="ticks", receive_model=Never, send_model=int)
    assert (one_way.receive_format, one_way.send_format) == ("none", "json")
    assert one_way.receive_adapter is None
    with pytest.raises(ValueError, match="neither receives nor sends"):
        DataChannelSpec(name="ticks", receive_model=Never, send_model=NoReturn)


def test_channel_specs_infer_message_types() -> None:
    class Prompt(BaseModel):
        text: str

    typed = DataChannelSpec("typed", receive_model=Prompt, send_model=bytes)
    one_way = DataChannelSpec("one_way", send_model=str)
    union: DataChannelSpec[int | str, Any] = DataChannelSpec("u", int | str)
    assert_type(typed, DataChannelSpec[Prompt, bytes])
    assert_type(one_way, DataChannelSpec[Any, str])
    assert_type(DataChannelSpec("raw"), DataChannelSpec[Any, Any])
    assert union.receive_format == "json"
    with pytest.raises(TypeError):
        DataChannelSpec("c", None, None, True)  # type: ignore[call-overload]


def test_message_handlers_accept_specs_and_names() -> None:
    spec = DataChannelSpec("chat", receive_model=str)

    async def handler(peer: Peer, message: str) -> None:
        return None

    session = Session(on_message={spec: handler, "other": handler})
    assert session.message_handlers == {"chat": handler, "other": handler}
    with pytest.raises(ValueError, match="more than one message handler"):
        Session(on_message={spec: handler, "chat": handler})
    with pytest.raises(TypeError, match="channel names or DataChannelSpecs"):
        Session(on_message={1: handler})


@pytest.mark.parametrize("retransmits", [0, 1])
@pytest.mark.parametrize("lifetime", [0, 1])
def test_reliability_options_are_exclusive_including_zero(
    retransmits: int, lifetime: int
) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        DataChannelSpec(
            name="c", max_retransmits=retransmits, max_packet_life_time_ms=lifetime
        )


@pytest.mark.parametrize("option", ["max_retransmits", "max_packet_life_time_ms"])
@pytest.mark.parametrize("value", [-1, 65536, 0.5, True])
def test_invalid_reliability_values(option: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"{option} must be an integer between"):
        DataChannelSpec(name="c", **{option: value})  # type: ignore[call-overload]


@pytest.mark.parametrize("option", ["max_retransmits", "max_packet_life_time_ms"])
@pytest.mark.parametrize("value", [0, 65535])
def test_reliability_boundaries(option: str, value: int) -> None:
    channel = (
        DataChannelSpec(name="c", max_retransmits=value)
        if option == "max_retransmits"
        else DataChannelSpec(name="c", max_packet_life_time_ms=value)
    )
    assert getattr(channel, option) == value


def test_declaration_channel_ids() -> None:
    spec = declaration(
        DataChannelSpec(name="a"),
        DataChannelSpec(name="b", negotiated=False),
        DataChannelSpec(name="c", id=7),
    )
    assert spec.channel_ids == {"a": 0, "b": None, "c": 7}
    assert spec.channel("c").id == 7
    with pytest.raises(KeyError):
        spec.channel("missing")
    in_band = spec.channel("b")
    assert spec.server_creates(spec.channel("a"))
    assert not spec.server_creates(in_band)
    offerer = declaration(
        DataChannelSpec(name="b", negotiated=False), role=Role.OFFERER
    )
    assert offerer.server_creates(offerer.channel("b"))
    custom = DataChannelSpec(name="d", max_message_bytes=10)
    assert declaration(custom).max_message_bytes(custom) == 10
    assert spec.max_message_bytes(spec.channel("a")) == 64 * 1024


@pytest.mark.parametrize(
    ("channels", "message"),
    [
        ((DataChannelSpec(name="a"), DataChannelSpec(name="a")), "duplicate channels"),
        ((DataChannelSpec(name="a"), DataChannelSpec(name="b", id=0)), "channel IDs"),
    ],
)
def test_invalid_declarations(
    channels: tuple[DataChannelSpec, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        declaration(*channels)


def test_invalid_session_names_and_limits() -> None:
    with pytest.raises(ValueError, match="identifier"):
        SessionDeclaration(
            name="1-bad",
            path="/",
            role=Role.ANSWERER,
            media=MediaPolicy(),
            channels=(),
            limits=SessionLimits(),
        )
    with pytest.raises(ValueError, match="max_sessions"):
        SessionDeclaration(
            name="ok",
            path="/",
            role=Role.ANSWERER,
            media=MediaPolicy(),
            channels=(),
            limits=SessionLimits(),
            max_sessions=0,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("negotiation_timeout_seconds", 0, "positive"),
        ("max_queued_messages", 0, "positive"),
        ("transport_loss_grace_seconds", -1, "negative"),
        ("max_pending_candidates", -1, "negative"),
        ("media_timeout_seconds", 0, "positive"),
    ],
)
def test_invalid_limits(field: str, value: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SessionLimits(**{field: value})  # type: ignore[arg-type]


def test_turn_rest_credentials() -> None:
    turn = TurnRestCredentials(
        secret="s3cret",
        turn_urls=("turns:turn.example:443?transport=tcp",),
        stun_urls=("stun:turn.example:3478",),
        internal_turn_urls=("turn:10.0.0.5:3478",),
        ttl_seconds=600,
        clock=lambda: 1_000.5,
    )
    servers = turn.for_user(42)
    stun, relay = servers.client
    assert stun == IceServer(urls=("stun:turn.example:3478",))
    assert relay.username == "1600:42"
    expected = base64.b64encode(
        hmac.new(b"s3cret", b"1600:42", hashlib.sha1).digest()
    ).decode()
    assert relay.credential == expected
    assert servers.engine is not None
    assert servers.engine[1].urls == ("turn:10.0.0.5:3478",)
    assert servers.for_engine == servers.engine
    assert "s3cret" not in repr(turn)
    public = TurnRestCredentials(secret="k", turn_urls=("turn:t",)).for_user("u")
    assert public.engine is None
    assert public.for_engine == public.client
    assert len(public.client) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"secret": "", "turn_urls": ("turn:t",)}, "secret"),
        ({"secret": "k", "turn_urls": ()}, "TURN URL"),
        ({"secret": "k", "turn_urls": ("turn:t",), "ttl_seconds": 10}, "ttl"),
    ],
)
def test_invalid_turn_credentials(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TurnRestCredentials(**kwargs)  # type: ignore[arg-type]


def test_turn_usernames_cannot_contain_separators() -> None:
    with pytest.raises(ValueError, match="':'"):
        TurnRestCredentials(secret="k", turn_urls=("turn:t",)).for_user("a:b")


async def test_resolve_ice_servers() -> None:
    context = SessionContext(name="s", session_id="id", websocket=None)  # type: ignore[arg-type]
    stun = IceServer(urls=("stun:a",))
    assert await resolve_ice_servers(None, context) == IceServers()
    assert await resolve_ice_servers([stun], context) == IceServers(client=(stun,))
    assert await resolve_ice_servers(lambda c: [stun], context) == IceServers(
        client=(stun,)
    )

    async def provider(c: SessionContext) -> IceServers:
        return IceServers(client=(stun,), engine=())

    assert (await resolve_ice_servers(provider, context)).engine == ()
    with pytest.raises(TypeError, match="IceServer"):
        await resolve_ice_servers(lambda c: ["stun:a"], context)  # type: ignore[arg-type]


def test_sdp_summary() -> None:
    summary = summarize(
        "v=0\r\na=sendonly\r\na=ice-ufrag:session\r\n"
        "m=audio 9 RTP 0\r\na=mid:a\r\n"
        "m=video 9 RTP 0\r\na=mid:v\r\na=recvonly\r\na=ice-ufrag:video\r\n"
        "m=video 0 RTP 0\r\na=mid:x\r\n"
        "m=application 9 DTLS/SCTP webrtc-datachannel\r\n"
    )
    audio, video, rejected, application = summary.sections
    assert (audio.mid, audio.direction, audio.ufrag) == ("a", "sendonly", "session")
    assert audio.author_sends and not audio.author_receives
    assert video.author_receives and video.ufrag == "video"
    assert rejected.rejected and not rejected.author_sends
    assert application.kind == "application"
    assert summary.ufrags == {"session", "video"}
    assert summary.sending("audio") == 1
    assert summary.sending("video") == 0


def test_error_categories() -> None:
    assert close_code(ErrorCode.CLOSED) == 1000
    assert close_code(ErrorCode.SESSION_LIMIT) == 1013
    assert close_code(ErrorCode.TRANSPORT_LOST) == 4000
    for code in ErrorCode:
        assert default_message(code)
    error = WebRtcError(ErrorCode.INVALID_MESSAGE, "custom")
    assert (error.code, error.message) == (ErrorCode.INVALID_MESSAGE, "custom")
    assert str(error) == "invalid_message: custom"
    assert SessionClosedError().code is ErrorCode.CLOSED
    assert isinstance(SessionClosedError(ErrorCode.SHUTDOWN), WebRtcError)


def test_stats_helpers() -> None:
    stats = ConnectionStats(
        connection_state=ConnectionState.CONNECTED,
        selected_candidate_pair=CandidatePairStats(
            local_type="host", remote_type="relay"
        ),
        inbound=(
            MediaStats(kind=MediaKind.AUDIO, packets=3),
            MediaStats(kind=MediaKind.AUDIO, packets=4),
            MediaStats(kind=MediaKind.VIDEO, packets=1),
        ),
    )
    assert stats.received(MediaKind.AUDIO) == 7
    assert stats.received(MediaKind.VIDEO) == 1
    assert stats.selected_candidate_pair is not None
    assert stats.selected_candidate_pair.relayed
    assert not CandidatePairStats(local_type="host", remote_type="srflx").relayed


@pytest.mark.parametrize(
    ("origins", "error"),
    [
        ("https://app.example.com", TypeError),
        (["app.example.com"], ValueError),
        (["https://app.example.com/path"], ValueError),
        (["https://"], ValueError),
    ],
)
def test_allowed_origins_are_validated(origins: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        WebRtcRouter(engine=FakeEngine(), allowed_origins=origins)  # type: ignore[arg-type]


def test_allowed_origins_are_normalized() -> None:
    rtc = WebRtcRouter(
        engine=FakeEngine(), allowed_origins=[" HTTPS://App.example.com:8443/ "]
    )
    assert rtc.allowed_origins == frozenset({"https://app.example.com:8443"})
    assert WebRtcRouter(engine=FakeEngine()).allowed_origins is None
