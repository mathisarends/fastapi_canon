import asyncio
import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi_canon.integrations.websockets import (
    Inject,
    RpcBinaryInput,
    RpcBinaryOutput,
    RpcChannel,
    RpcConnection,
    RpcConnectionClose,
    RpcDisconnect,
    RpcService,
)
from fastapi_canon.integrations.websockets.codegen import generate_python_client
from fastapi_canon.integrations.websockets.codegen.python import PythonClientOptions
from fastapi_canon.integrations.websockets.websocket import (
    CLOSE_CODES,
    REJECTION_CLOSE_CODES,
)

from ..testing import InMemorySocket

PACKAGE = "media_client"


class Recording:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.completed = False
        self.finished = asyncio.Event()


uploads = RpcChannel("uploads")
voice = RpcChannel("voice")


@uploads.stream("audio", input_content_type="audio/pcm")
async def upload_audio(
    frames: Inject[RpcBinaryInput], recording: Inject[Recording]
) -> None:
    try:
        async for frame in frames:
            if frame == b"fail":
                raise RuntimeError("rejected frame")
            recording.frames.append(frame)
        recording.completed = True
    finally:
        recording.finished.set()


@voice.stream("media", content_type="audio/opus", input_content_type="audio/pcm")
async def media(
    voice_session_id: UUID,
    frames: Inject[RpcBinaryInput],
    output: Inject[RpcBinaryOutput],
    connection: Inject[RpcConnection],
) -> None:
    if voice_session_id.int == 0:
        await connection.close(
            RpcConnectionClose.POLICY_VIOLATION, reason="Session has an owner"
        )
        return
    await output.send(b"ready")
    async for frame in frames:
        await output.send(frame.upper())
    await output.send(b"bye")


service = RpcService()
service.stream("/uploads/audio", upload_audio)
service.stream("/voice/{voice_session_id}/media", media)


@pytest.fixture(scope="module")
def client_package(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    root = tmp_path_factory.mktemp("stream_client")
    document = service.contract(title="Media", base_url="ws://localhost").to_openrpc()
    generate_python_client(
        document,
        root / PACKAGE,
        PythonClientOptions(package=PACKAGE, client_name="MediaClient"),
    )
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


class LoopbackStream:
    """A stream transport served by the real runtime over an in-memory socket."""

    def __init__(self, package: ModuleType, path: str, recording: Recording) -> None:
        self._package = package
        self.socket = InMemorySocket(path)
        self.task = asyncio.create_task(service.serve(self.socket, context=recording))

    async def receive(self) -> bytes:
        try:
            return await self.socket.client_receive()
        except RpcDisconnect:
            if self.socket.rejection is not None:
                code = REJECTION_CLOSE_CODES[self.socket.rejection[0]]
                reason = self.socket.rejection[1]
            else:
                close, reason = self.socket.closed or (RpcConnectionClose.NORMAL, "")
                code = CLOSE_CODES[close]
            if code == 1000:
                raise self._package.RpcStreamClosed("closed") from None
            failure = (
                self._package.RpcStreamRefused
                if code == 1008
                else self._package.RpcStreamFailed
            )
            raise failure(code, reason) from None

    async def send(self, frame: bytes) -> None:
        await self.socket.client_send(frame)

    async def end_input(self) -> None:
        end = self._package.BinaryInputEnd(type="end")
        await self.socket.client_send(end.model_dump_json())

    async def close(self) -> None:
        await self.socket.client_disconnect()
        await self.task


class UnusedTransport:
    async def close(self) -> None: ...


def _client(package: ModuleType, recording: Recording, opened: list | None = None):
    async def opener(endpoint):
        stream = LoopbackStream(package, urlsplit(endpoint.url).path, recording)
        if opened is not None:
            opened.append((endpoint, stream))
        return stream

    return package.MediaClient.with_transports(UnusedTransport(), stream_opener=opener)


async def test_sink_sends_frames_and_ends_on_normal_exit(
    client_package: ModuleType,
) -> None:
    recording = Recording()
    opened: list = []
    client = _client(client_package, recording, opened)

    async with client.uploads.audio() as sink:
        assert isinstance(sink, client_package.BinarySender)
        await sink.send(b"one")
        await sink.send(memoryview(b"two"))

    endpoint, stream = opened[0]
    assert endpoint.direction == "client-to-server"
    assert endpoint.content_type is None
    assert endpoint.input_content_type == "audio/pcm"
    assert recording.frames == [b"one", b"two"]
    assert recording.completed
    assert stream.socket.closed == (RpcConnectionClose.NORMAL, "")


async def test_sink_end_reports_server_failures(client_package: ModuleType) -> None:
    client = _client(client_package, Recording())

    with pytest.raises(client_package.RpcStreamFailed) as failure:
        async with client.uploads.audio() as sink:
            await sink.send(b"fail")

    assert failure.value.code == 1011
    assert failure.value.reason == "Internal error"


async def test_sink_end_gives_up_when_the_server_never_closes(
    client_package: ModuleType,
) -> None:
    class SilentTransport:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def send(self, frame: bytes) -> None: ...

        async def end_input(self) -> None: ...

        async def receive(self) -> bytes:
            await self.closed.wait()
            raise client_package.RpcStreamClosed("closed")

        async def close(self) -> None:
            self.closed.set()

    transport = SilentTransport()

    async def opener(endpoint):
        return transport

    client = client_package.MediaClient.with_transports(
        UnusedTransport(), stream_opener=opener
    )

    with pytest.raises(TimeoutError):
        async with client.uploads.audio() as sink:
            await sink.end(timeout=0.01)

    assert transport.closed.is_set()


async def test_leaving_a_sink_with_an_exception_aborts_the_upload(
    client_package: ModuleType,
) -> None:
    recording = Recording()
    client = _client(client_package, recording)

    with pytest.raises(LookupError):
        async with client.uploads.audio() as sink:
            await sink.send(b"partial")
            await asyncio.sleep(0)
            raise LookupError

    await recording.finished.wait()
    assert recording.frames == [b"partial"]
    assert not recording.completed


async def test_duplex_streams_send_and_receive_concurrently(
    client_package: ModuleType,
) -> None:
    opened: list = []
    client = _client(client_package, Recording(), opened)
    session_id = uuid4()

    async with client.voice.media(voice_session_id=str(session_id)) as duplex:
        assert isinstance(duplex, client_package.BinaryChannel)
        assert await duplex.receive() == b"ready"
        await duplex.send(b"hello")
        assert await duplex.receive() == b"HELLO"
        await duplex.end_input()
        with pytest.raises(client_package.RpcStreamClosed):
            await duplex.send(b"late")
        assert [frame async for frame in duplex] == [b"bye"]

    endpoint, _ = opened[0]
    assert endpoint.url == f"ws://localhost/voice/{session_id}/media"
    assert endpoint.direction == "bidirectional"
    assert endpoint.content_type == "audio/opus"


async def test_policy_violations_surface_as_refused_streams(
    client_package: ModuleType,
) -> None:
    client = _client(client_package, Recording())

    with pytest.raises(client_package.RpcStreamRefused) as refused:
        async with client.voice.media(voice_session_id=str(UUID(int=0))) as duplex:
            await duplex.receive()

    assert refused.value.code == 1008
    assert refused.value.reason == "Session has an owner"


async def test_openers_without_send_cannot_open_input_streams(
    client_package: ModuleType,
) -> None:
    class ReceiveOnly:
        closed = False

        async def receive(self) -> bytes:
            return b""

        async def close(self) -> None:
            self.closed = True

    transport = ReceiveOnly()

    async def opener(endpoint):
        return transport

    client = client_package.MediaClient.with_transports(
        UnusedTransport(), stream_opener=opener
    )

    with pytest.raises(client_package.RpcStreamsUnavailableError, match="send"):
        async with client.uploads.audio():
            pass
    assert transport.closed
