from collections.abc import AsyncIterator

import pytest
from fastapi_canon.integrations.websockets import (
    ProtocolDefinitionError,
    RpcChannel,
    RpcError,
    RpcModel,
    RpcServer,
    RpcService,
)


class SharedError(RpcError):
    pass


class LocalError(RpcError):
    pass


class Event(RpcModel):
    value: str


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_method_deadline_requires_positive_finite_seconds(timeout):
    with pytest.raises(ProtocolDefinitionError, match="timeout"):
        RpcChannel("limits").method(timeout=timeout)


def test_method_deadline_keeps_inheritance_distinct_from_explicit_none():
    channel = RpcChannel("limits")

    @channel.method()
    async def inherited() -> None: ...

    @channel.method(timeout=None)
    async def unbounded() -> None: ...

    @channel.method(timeout=5)
    async def bounded() -> None: ...

    first, second, third = channel.freeze().methods
    assert first.timeout is None and not first._has_timeout
    assert second.timeout is None and second._has_timeout
    assert third.timeout == 5 and third._has_timeout


def test_control_method_prefix_is_reserved():
    with pytest.raises(ProtocolDefinitionError):
        RpcChannel("controls").method("$/cancelRequest")


def test_channel_defaults_and_bare_decorators() -> None:
    channel = RpcChannel("control", raises=[SharedError])

    @channel.method
    async def ping() -> None: ...

    @channel.method(raises=[LocalError])
    async def other() -> None: ...

    assert channel.namespace == "control"
    assert channel.routes[0].name == "control.ping"
    assert channel.routes[1].raises == (SharedError, LocalError)


def test_event_and_stream_inference() -> None:
    channel = RpcChannel("events")

    @channel.event
    async def changed() -> AsyncIterator[Event]:
        yield Event(value="x")

    @channel.stream(content_type="image/png")
    async def frames() -> AsyncIterator[bytes]:
        """Published frames."""
        yield b"x"

    assert channel.events[0].payload is Event
    assert channel.streams[0].name == "events.frames"
    assert channel.streams[0].summary == "Published frames."


def test_freeze_prevents_registration() -> None:
    channel = RpcChannel("control", namespace="")
    channel.freeze()
    with pytest.raises(ProtocolDefinitionError, match="frozen"):
        channel.method()


def test_invalid_stream_type_is_rejected() -> None:
    channel = RpcChannel("files")
    with pytest.raises(ProtocolDefinitionError, match="must yield bytes"):

        @channel.stream()
        async def text() -> AsyncIterator[str]:
            yield "x"


def test_child_channels_inherit_namespace_errors_and_scope() -> None:
    root = RpcChannel("voice", raises=(SharedError,))
    turn = root.child("turn", raises=(LocalError,))

    @root.event()
    async def event() -> AsyncIterator[Event]:
        yield Event(value="root")

    @turn.method()
    async def start() -> None: ...

    protocol = root.freeze()

    assert turn.name == "voice.turn"
    assert turn.namespace == "voice.turn"
    assert turn.raises == (SharedError, LocalError)
    assert protocol.methods[0].name == "voice.turn.start"
    assert protocol.methods[0].raises == (SharedError, LocalError)
    assert protocol.notifications[0].name == "voice.event"


def test_channel_name_defaults_to_namespace() -> None:
    channel = RpcChannel(namespace="voice.turn")

    assert channel.name == "voice.turn"
    assert channel.namespace == "voice.turn"


def test_dotted_names_are_relative_to_the_channel_namespace() -> None:
    channel = RpcChannel("input", namespace="browser.input", raises=[SharedError])
    nested = channel.child("pointer.touch")

    @channel.method("text.insert")
    async def insert_text() -> None: ...

    @channel.event("text.changed")
    async def text_changed() -> AsyncIterator[Event]:
        yield Event(value="x")

    @channel.subscription("text.watch")
    async def watch_text() -> AsyncIterator[Event]:
        yield Event(value="x")

    @channel.stream("screen.frames")
    async def screen_frames() -> AsyncIterator[bytes]:
        yield b""

    protocol = channel.freeze()

    assert protocol.methods[0].name == "browser.input.text.insert"
    assert protocol.methods[0].raises == (SharedError,)
    assert protocol.methods[0].request_name == "InsertTextRequest"
    assert protocol.notifications[0].name == "browser.input.text.changed"
    assert protocol.subscriptions[0].name == "browser.input.text.watch"
    assert protocol.streams[0].name == "browser.input.screen.frames"
    assert nested.namespace == "browser.input.pointer.touch"


@pytest.mark.parametrize("name", ["text..insert", ".insert", "text.", "text.1st", ""])
def test_each_segment_of_a_dotted_name_is_validated(name: str) -> None:
    channel = RpcChannel("input")

    with pytest.raises(ProtocolDefinitionError, match="Invalid RPC method name"):
        channel.method(name)


def test_dotted_name_and_child_route_cannot_share_a_wire_name() -> None:
    channel = RpcChannel("input")

    @channel.method("text.insert")
    async def insert_text() -> None: ...

    @channel.child("text").method("insert")
    async def insert() -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="Duplicate RPC method"):
        channel.freeze()


def test_dotted_name_cannot_turn_an_operation_into_a_namespace() -> None:
    channel = RpcChannel("input")

    @channel.method
    async def text() -> None: ...

    @channel.method("text.insert")
    async def insert_text() -> None: ...

    service = RpcService()
    service.socket("/rpc", channels=(channel,))
    with pytest.raises(ProtocolDefinitionError, match="both an operation"):
        service.freeze()


def test_channel_declarations_are_served_by_its_server() -> None:
    channel = RpcChannel("room")

    @channel.method
    async def join() -> None: ...

    @channel.event("joined")
    async def joined_events() -> AsyncIterator[Event]:
        yield Event(value="x")

    assert [route.name for route in channel.routes] == ["room.join"]
    assert [event.name for event in channel.events] == ["room.joined"]
    assert isinstance(channel.create_server(), RpcServer)


def test_channel_decorators_declare_server_operations() -> None:
    channel = RpcChannel("room")

    @channel.method
    async def join() -> None: ...

    @channel.method("leave", summary="Leave the room.")
    async def leave_room() -> None: ...

    @channel.event("joined")
    async def joined_events() -> AsyncIterator[Event]:
        yield Event(value="x")

    @channel.subscription
    async def members() -> AsyncIterator[Event]:
        yield Event(value="x")

    @channel.stream(content_type="audio/pcm")
    async def audio() -> AsyncIterator[bytes]:
        yield b""

    assert [route.name for route in channel.routes] == ["room.join", "room.leave"]
    assert channel.routes[1].summary == "Leave the room."
    assert [event.name for event in channel.events] == ["room.joined"]
    assert [item.name for item in channel.subscriptions] == ["room.members"]
    assert [stream.name for stream in channel.streams] == ["room.audio"]


def test_channel_rejects_duplicate_route_names() -> None:
    channel = RpcChannel("room")

    @channel.method
    async def join() -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="Duplicate RPC route"):

        @channel.method("join")
        async def join_again() -> None: ...


def test_channel_has_no_client_or_server_side() -> None:
    channel = RpcChannel("room")

    assert not hasattr(channel, "client")
    assert not hasattr(channel, "server")
