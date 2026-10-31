import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    ProtocolDefinitionError,
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcChannel,
    RpcConnection,
    RpcConnectionClose,
    RpcError,
    RpcInputEnded,
    RpcLimits,
    RpcReject,
    RpcRejection,
    RpcService,
    RpcStreamClose,
    RpcStreamDirection,
)

from .testing import RpcTestClient, RpcTestConnectionClosed


class Recording:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.completed = False
        self.released = asyncio.Event()


class MediaOwners:
    def __init__(self) -> None:
        self._owners: set[UUID] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def claim(self, key: UUID) -> AsyncGenerator[bool, None]:
        async with self._lock:
            claimed = key not in self._owners
            if claimed:
                self._owners.add(key)
        try:
            yield claimed
        finally:
            if claimed:
                self._owners.discard(key)


def _service(*streams, path: str = "/stream") -> RpcService:
    service = RpcService()
    for stream in streams:
        service.stream(path, stream)
    return service


def test_direction_is_derived_from_the_handler_signature() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def generated() -> AsyncIterator[bytes]:
        yield b""

    @channel.stream()
    async def pushed(output: Inject[RpcBinaryOutput]) -> None: ...

    @channel.stream(input_content_type="audio/pcm")
    async def uploaded(frames: Inject[RpcBinaryInput]) -> None: ...

    @channel.stream(content_type="audio/pcm")
    async def duplex(
        frames: Inject[RpcBinaryInput], output: Inject[RpcBinaryOutput]
    ) -> None: ...

    streams = {stream.name: stream for stream in channel.streams}
    assert streams["media.generated"].direction is RpcStreamDirection.SERVER_TO_CLIENT
    assert streams["media.pushed"].direction is RpcStreamDirection.SERVER_TO_CLIENT
    assert streams["media.uploaded"].direction is RpcStreamDirection.CLIENT_TO_SERVER
    assert streams["media.uploaded"].input_content_type == "audio/pcm"
    assert streams["media.duplex"].direction is RpcStreamDirection.BIDIRECTIONAL
    assert streams["media.duplex"].input_content_type == "audio/pcm"
    assert streams["media.generated"].input_content_type is None


def test_generator_with_input_points_to_the_coroutine_form() -> None:
    channel = RpcChannel("media")

    with pytest.raises(ProtocolDefinitionError, match="await output.send"):

        @channel.stream()
        async def invalid(frames: Inject[RpcBinaryInput]) -> AsyncIterator[bytes]:
            yield b""


def test_input_content_type_requires_input() -> None:
    channel = RpcChannel("media")

    with pytest.raises(ProtocolDefinitionError, match="input_content_type"):

        @channel.stream(input_content_type="audio/pcm")
        async def invalid() -> AsyncIterator[bytes]:
            yield b""


def test_coroutine_streams_need_binary_input_or_output() -> None:
    channel = RpcChannel("media")

    with pytest.raises(ProtocolDefinitionError, match="RpcBinaryInput and/or"):

        @channel.stream()
        async def invalid() -> None: ...


def test_stream_path_parameters_must_be_path_variables() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def upload(session_id: UUID, frames: Inject[RpcBinaryInput]) -> None: ...

    with pytest.raises(ProtocolDefinitionError, match="session_id"):
        RpcService().stream("/uploads/{upload_id}", upload)


def test_stream_path_parameters_reject_models() -> None:
    channel = RpcChannel("media")

    with pytest.raises(ProtocolDefinitionError, match="path variable"):

        @channel.stream()
        async def upload(session: Recording, frames: Inject[RpcBinaryInput]) -> None:
            pass


def test_contract_describes_direction_and_input_content_type() -> None:
    channel = RpcChannel("media")

    @channel.stream(input_content_type="audio/pcm")
    async def upload(frames: Inject[RpcBinaryInput]) -> None: ...

    @channel.stream(content_type="audio/opus", input_content_type="audio/pcm")
    async def talk(
        frames: Inject[RpcBinaryInput], output: Inject[RpcBinaryOutput]
    ) -> None: ...

    service = RpcService()
    service.stream("/upload", upload)
    service.stream("/talk", talk)
    streams = {
        item["name"]: item
        for item in service.contract(
            title="Media", base_url="ws://localhost"
        ).binary_streams
    }

    assert dict(streams["media.upload"]) == {
        "name": "media.upload",
        "url": "ws://localhost/upload",
        "direction": "client-to-server",
        "inputContentType": "audio/pcm",
        "frameType": "binary",
    }
    assert streams["media.talk"]["direction"] == "bidirectional"
    assert streams["media.talk"]["contentType"] == "audio/opus"
    assert streams["media.talk"]["inputContentType"] == "audio/pcm"


async def test_client_to_server_frames_arrive_in_order_until_end() -> None:
    channel = RpcChannel("uploads")
    recording = Recording()

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput], target: Inject[Recording]) -> None:
        async for frame in frames:
            target.frames.append(frame)
        assert frames.ended
        with pytest.raises(RpcInputEnded):
            await frames.receive()
        target.completed = True

    async with RpcTestClient(_service(upload), "/stream", context=recording) as client:
        await client.send_frame(b"one")
        await client.send_frame(bytearray(b"two"))
        await client.end_input()
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")

    assert recording.frames == [b"one", b"two"]
    assert recording.completed


async def test_bidirectional_output_continues_after_input_end() -> None:
    channel = RpcChannel("voice")

    @channel.stream()
    async def echo(
        frames: Inject[RpcBinaryInput], output: Inject[RpcBinaryOutput]
    ) -> None:
        received = [frame async for frame in frames]
        await output.send(b"got " + b",".join(received))
        await output.send(memoryview(b"done"))

    async with RpcTestClient(_service(echo), "/stream") as client:
        await client.send_frame(b"a")
        await client.send_frame(b"b")
        await client.end_input()
        assert await client.next_frame() == b"got a,b"
        assert await client.next_frame() == b"done"
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")


async def test_bidirectional_input_and_output_run_concurrently() -> None:
    channel = RpcChannel("voice")

    @channel.stream()
    async def echo(
        frames: Inject[RpcBinaryInput], output: Inject[RpcBinaryOutput]
    ) -> None:
        async for frame in frames:
            await output.send(frame.upper())

    async with RpcTestClient(_service(echo), "/stream") as client:
        await client.send_frame(b"a")
        assert await client.next_frame() == b"A"
        await client.send_frame(b"b")
        assert await client.next_frame() == b"B"
        await client.end_input()
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")


async def test_push_based_output_without_input() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def ticks(output: Inject[RpcBinaryOutput]) -> None:
        for value in (b"1", b"2"):
            await output.send(value)

    async with RpcTestClient(_service(ticks), "/stream") as client:
        assert await client.next_frame() == b"1"
        assert await client.next_frame() == b"2"
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")
        with pytest.raises(TypeError, match="send_frame"):
            await client.send_frame(b"x")


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        ((b"a", '{"type":"end"}', b"late"), "Binary input after end"),
        (('{"type":"stop"}',), "Unexpected text frame on a binary stream"),
        (("not json",), "Unexpected text frame on a binary stream"),
        (("{}",), "Unexpected text frame on a binary stream"),
        (('{"type":"end","reason":"x"}',), "Unexpected text frame"),
        (('{"type":"end"}', '{"type":"end"}'), "Unexpected text frame"),
    ],
)
async def test_invalid_input_is_a_protocol_error(frames, expected) -> None:
    channel = RpcChannel("uploads")

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput]) -> None:
        async for _ in frames:
            pass
        await asyncio.Event().wait()

    async with RpcTestClient(_service(upload), "/stream") as client:
        for frame in frames:
            await client.socket.client_send(frame)
        code, reason = await client.closed()

    assert code is RpcConnectionClose.PROTOCOL_ERROR
    assert reason.startswith(expected)


async def test_input_on_a_server_to_client_stream_is_a_protocol_error() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""

    async with RpcTestClient(_service(frames), "/stream") as client:
        await client.socket.client_send('{"type":"end"}')
        assert await client.closed() == (
            RpcConnectionClose.PROTOCOL_ERROR,
            "Binary stream is server-to-client",
        )


async def test_oversized_input_frames_close_the_stream() -> None:
    channel = RpcChannel("uploads")

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput]) -> None:
        async for _ in frames:
            pass

    async with RpcTestClient(
        _service(upload), "/stream", limits=RpcLimits(max_message_bytes=4)
    ) as client:
        await client.send_frame(b"12345")
        assert await client.closed() == (RpcConnectionClose.MESSAGE_TOO_BIG, "")


@pytest.mark.parametrize("code", [RpcConnectionClose.NORMAL, 1006])
async def test_disconnect_before_end_aborts_the_handler(code) -> None:
    channel = RpcChannel("uploads")
    recording = Recording()

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput], target: Inject[Recording]) -> None:
        try:
            async for frame in frames:
                target.frames.append(frame)
            target.completed = True
        finally:
            target.released.set()

    async with RpcTestClient(_service(upload), "/stream", context=recording) as client:
        await client.send_frame(b"partial")
        await client.socket.client_disconnect(code)
        await client.closed()

    assert recording.released.is_set()
    assert recording.frames == [b"partial"]
    assert not recording.completed


async def test_reader_applies_backpressure_with_a_bounded_queue() -> None:
    channel = RpcChannel("uploads")
    gate = asyncio.Event()
    received: list[bytes] = []

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput]) -> None:
        await gate.wait()
        received.extend([frame async for frame in frames])

    async with RpcTestClient(
        _service(upload), "/stream", limits=RpcLimits(max_queue_size=2)
    ) as client:
        for index in range(5):
            await client.send_frame(bytes([index]))
        await client.end_input()
        for _ in range(10):
            await asyncio.sleep(0)
        # Two frames are queued for the handler and one is held by the reader;
        # the rest stays with the transport until the handler reads.
        assert client.socket._incoming.qsize() == 3
        gate.set()
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")

    assert received == [bytes([index]) for index in range(5)]


async def test_path_variables_are_validated_and_typed() -> None:
    channel = RpcChannel("voice")
    seen: list[UUID] = []

    @channel.stream()
    async def media(voice_session_id: UUID, output: Inject[RpcBinaryOutput]) -> None:
        seen.append(voice_session_id)
        await output.send(voice_session_id.bytes)

    service = _service(media, path="/voice/{voice_session_id}/media")
    session_id = uuid4()
    async with RpcTestClient(service, f"/voice/{session_id}/media") as client:
        assert await client.next_frame() == session_id.bytes
    assert seen == [session_id]

    async with RpcTestClient(service, "/voice/not-a-uuid/media") as client:
        await client.closed()
        assert not client.socket.accepted
        assert client.socket.rejection == (
            RpcRejection.NOT_FOUND,
            "Invalid path variable",
        )
        with pytest.raises(RpcTestConnectionClosed):
            await client.next_frame()


async def test_ownership_recipe_refuses_a_second_connection() -> None:
    channel = RpcChannel("voice")

    @channel.stream()
    async def media(
        voice_session_id: UUID,
        frames: Inject[RpcBinaryInput],
        output: Inject[RpcBinaryOutput],
        connection: Inject[RpcConnection],
        owners: Inject[MediaOwners],
    ) -> None:
        async with owners.claim(voice_session_id) as claimed:
            if not claimed:
                await connection.close(
                    RpcConnectionClose.POLICY_VIOLATION,
                    reason="Voice session already has a media owner",
                )
                return
            async for frame in frames:
                await output.send(frame)

    service = _service(media, path="/voice/{voice_session_id}/media")
    owners = MediaOwners()
    path = f"/voice/{uuid4()}/media"
    async with (
        RpcTestClient(service, path, context=owners) as first,
        RpcTestClient(service, path, context=owners) as second,
    ):
        await first.send_frame(b"ping")
        assert await first.next_frame() == b"ping"
        assert await second.closed() == (
            RpcConnectionClose.POLICY_VIOLATION,
            "Voice session already has a media owner",
        )
        await first.send_frame(b"still")
        assert await first.next_frame() == b"still"

    async with RpcTestClient(service, path, context=owners) as third:
        await third.send_frame(b"free")
        assert await third.next_frame() == b"free"


async def test_handler_failures_close_with_internal_error() -> None:
    channel = RpcChannel("uploads")

    @channel.stream()
    async def upload(frames: Inject[RpcBinaryInput]) -> None:
        await frames.receive()
        raise RuntimeError("boom")

    async with RpcTestClient(_service(upload), "/stream") as client:
        await client.send_frame(b"x")
        assert await client.closed() == (
            RpcConnectionClose.INTERNAL_ERROR,
            "Internal error",
        )


async def test_stream_handler_can_request_a_close() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        raise RpcStreamClose(RpcConnectionClose.POLICY_VIOLATION, "Unavailable")
        yield b"never"

    async with RpcTestClient(_service(frames), "/stream") as client:
        assert await client.closed() == (
            RpcConnectionClose.POLICY_VIOLATION,
            "Unavailable",
        )


async def test_stream_error_mapper_maps_close_reason() -> None:
    class MissingError(RpcError):
        pass

    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        raise ValueError("missing")
        yield b"never"

    service = RpcService()
    service.stream(
        "/stream",
        frames,
        error_mapper=lambda error: (
            MissingError(message=str(error)) if isinstance(error, ValueError) else None
        ),
    )
    async with RpcTestClient(service, "/stream") as client:
        assert await client.closed() == (
            RpcConnectionClose.POLICY_VIOLATION,
            "missing: missing",
        )


class AccessRevoked(Exception):
    pass


@pytest.mark.parametrize(
    ("rejections", "expected"),
    [
        (
            {AccessRevoked: RpcRejection.FORBIDDEN},
            (RpcConnectionClose.POLICY_VIOLATION, "revoked"),
        ),
        (
            lambda error: RpcReject(RpcRejection.UNAVAILABLE, "later"),
            (RpcConnectionClose.TRY_AGAIN_LATER, "later"),
        ),
        (
            lambda error: None,
            (RpcConnectionClose.INTERNAL_ERROR, "Internal error"),
        ),
    ],
)
async def test_stream_failures_close_with_mapped_rejections(
    rejections, expected
) -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        yield b"first"
        raise AccessRevoked("revoked")

    async with RpcTestClient(
        _service(frames), "/stream", rejections=rejections
    ) as client:
        assert await client.next_frame() == b"first"
        assert await client.closed() == expected


async def test_stream_raising_a_rejection_closes_with_its_code() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        raise RpcReject(RpcRejection.NOT_FOUND, "gone")
        yield b"never"

    async with RpcTestClient(_service(frames), "/stream") as client:
        assert await client.closed() == (RpcConnectionClose.POLICY_VIOLATION, "gone")


async def test_failing_rejection_mapper_closes_with_internal_error() -> None:
    channel = RpcChannel("media")

    @channel.stream()
    async def frames() -> AsyncIterator[bytes]:
        raise AccessRevoked()
        yield b"never"

    def broken(error: Exception) -> RpcReject | None:
        raise RuntimeError("mapper bug")

    async with RpcTestClient(_service(frames), "/stream", rejections=broken) as client:
        assert await client.closed() == (
            RpcConnectionClose.INTERNAL_ERROR,
            "Internal error",
        )


async def test_output_after_close_raises_instead_of_hanging() -> None:
    channel = RpcChannel("media")
    errors: list[Exception] = []

    @channel.stream()
    async def push(
        output: Inject[RpcBinaryOutput], connection: Inject[RpcConnection]
    ) -> None:
        await connection.close()
        try:
            await output.send(b"late")
        except Exception as error:
            errors.append(error)

    async with RpcTestClient(_service(push), "/stream") as client:
        assert await client.closed() == (RpcConnectionClose.NORMAL, "")

    assert len(errors) == 1


def test_binary_endpoints_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        RpcBinaryInput()
    with pytest.raises(TypeError):
        RpcBinaryOutput()
