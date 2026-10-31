# Canon and Dishka

Register `WebRtcRouter` directly in `Feature.routers`. Canon installs Dishka's
middleware and manages the application container. WebRTC does not need a Canon
adapter or a required dependency on Canon or Dishka.

For declarative admission errors, the optional
[`CanonWebRtcRouter`](../../fastapi-canon/docs/webrtc.md) adds Canon's registry and
router/session `raises` declarations. It exports HTTP denial metadata in the
WebRTC contract while preserving the native session runtime and signaling protocol.
Register a plain WebRTC feature's registry explicitly with `Feature(errors=...)`
when using runtime exception mapping without that integration.

For typed browser denials, see the optional [HTTP ticket recipe](admission-tickets.md).
For remote peers, the [send-only example](../examples/remote_video/README.md)
binds a separate engine and lease to each session, including refused sessions.

The session factory is a FastAPI dependency. Decorate an **async factory** with
`@inject` below `@rtc.session(...)`; Canon does not automatically inject this
factory. Session services use `Scope.SESSION`, matching Dishka's
[WebSocket scope model](https://dishka.readthedocs.io/en/stable/integrations/fastapi.html#websockets).

```python
from collections.abc import AsyncIterator

from dishka import AsyncContainer, FromDishka, Provider, Scope, provide
from dishka.integrations.fastapi import inject
from fastapi import FastAPI
from fastapi_canon import Composition, Feature
from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Peer,
    Session,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine


class Transcript:
    def __init__(self) -> None:
        self.messages: list[str] = []


class MessageWork:
    def reply(self, text: str, transcript: Transcript) -> str:
        transcript.messages.append(text)
        return text.upper()


class Services(Provider):
    @provide(scope=Scope.SESSION)
    async def transcript(self) -> AsyncIterator[Transcript]:
        transcript = Transcript()
        yield transcript
        # Persist or release session resources here, after on_close.

    @provide(scope=Scope.REQUEST)
    async def work(self) -> AsyncIterator[MessageWork]:
        yield MessageWork()
        # Release this message's transaction or other short-lived resources.


rtc = WebRtcRouter(engine=AiortcEngine())


CHAT = DataChannelSpec("chat", receive_model=str, send_model=str)


@rtc.session("/chat", data_channels=[CHAT])
@inject
async def chat(
    transcript: FromDishka[Transcript], container: FromDishka[AsyncContainer]
) -> Session:
    async def on_message(peer: Peer, text: str) -> None:
        async with container(scope=Scope.REQUEST) as request:
            work = await request.get(MessageWork)
            await peer.send_message(CHAT, work.reply(text, transcript))

    return Session(on_message={CHAT: on_message})


app = Composition(Feature(name="chat", routers=[rtc], providers=[Services])).apply(
    FastAPI()
)
```

Callbacks are called directly with the documented `Peer`, message, track or
close-code arguments. Capture session services in the factory's closures;
adding `FromDishka[...]` callback parameters does not inject them. For resources
per message, enter a new child `Scope.REQUEST` and resolve services explicitly,
as above. Await all work using those resources before leaving the child scope.

Accepted sessions call `on_close` while session services remain available.
Owned tracks and engine peers are then released before the session dependency
scope exits, including when the ASGI task is cancelled during cleanup. Canon
runs feature-router shutdown before closing feature resources and its
application container.

FastAPI authentication dependencies can deny admission before the factory runs.
Capacity and router-shutdown checks happen after factory resolution: if a
returned `Session` is refused, its owned `tracks` are stopped before dependency
teardown, without calling `on_close` or allocating an engine peer. Factories
must clean up resources they create if they raise before returning a `Session`.

The executable [integration tests](../../../tests/integration/test_webrtc_dishka.py)
cover HTTP admission denial, capacity rejection, disconnect, application
shutdown, cleanup order and two real aiortc data-channel messages with distinct
request scopes sharing one session service.
