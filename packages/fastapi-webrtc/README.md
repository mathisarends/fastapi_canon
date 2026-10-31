# Canon WebRTC

This directory retains feature tests, examples and guides. The implementation
now ships in `fastapi-canon[webrtc]` (prepared 0.9.0, not yet published).
See the [public API](../../docs/canon.md#public-namespaces) and
[package migration guide](../fastapi-canon/docs/package-migration.md).

Typed WebRTC sessions for FastAPI. You declare a session like a route: its
factory is an ordinary FastAPI dependency, so authentication, path and query
parameters and `yield` cleanup work as usual. They all run before any peer
connection exists. The package exports a contract and generates a typed,
framework-free browser client from it.

```console
pip install "fastapi-canon[webrtc,aiortc]"
```

The `webrtc` extra selects Starlette >=0.41 and the other transport requirements. The Starlette
lower bound ensures that an admission dependency's `HTTPException` returns an
HTTP response before accepting the WebSocket. Media engines are adapters; the
`aiortc` extra installs the reference engine. The implementation ships in Canon; it does not load other transport modules or GStreamer.

## Server

```python
from typing import Annotated

from aiortc.contrib.media import MediaRelay
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from fastapi_canon.integrations.webrtc import (
    DataChannelSpec,
    Direction,
    IncomingTrack,
    MediaPolicy,
    Peer,
    Session,
    WebRtcRouter,
)
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine


class Prompt(BaseModel):
    text: str


class Feedback(BaseModel):
    text: str
    words: int


# The spec types the channel handles: `peer.channel(MESSAGES)` receives
# `Prompt` and sends `Feedback`.
MESSAGES = DataChannelSpec("messages", receive_model=Prompt, send_model=Feedback)


def lesson_access(lesson_id: str, token: Annotated[str, Query()]) -> str:
    if token != "demo":
        raise HTTPException(status_code=403)  # HTTP 403; no peer is created.
    return lesson_id


rtc = WebRtcRouter(prefix="/rtc", engine=AiortcEngine(), max_sessions=50)


@rtc.session(
    "/lessons/{lesson_id}",
    name="practice",
    media=MediaPolicy(audio=Direction.SEND_RECEIVE),
    data_channels=(MESSAGES,),
)
def practice(lesson: Annotated[str, Depends(lesson_access)]) -> Session:
    relay = MediaRelay()

    async def on_track(peer: Peer, track: IncomingTrack) -> None:
        await peer.send_track(relay.subscribe(track.native))  # Echo the microphone.

    async def on_prompt(peer: Peer, prompt: Prompt) -> None:
        await peer.send_message(
            MESSAGES, Feedback(text=prompt.text, words=len(prompt.text.split()))
        )

    return Session(on_track=on_track, on_message={MESSAGES: on_prompt})


app = FastAPI()
app.include_router(rtc)
```

A `Session` is assembled from handlers:

- `on_start(peer)`: runs once the transport is connected and channels are open.
- `run(peer)`: the session's main task. When it returns, the session closes normally.
- `on_track(peer, track)`: called for each incoming track. `track.recv()` or
  `async for frame in track` reads frames.
- `on_message={spec: handler}`: validated messages per channel, keyed by its
  `DataChannelSpec` or its name. Without a handler, a channel can be consumed
  with `async for message in peer.channel(spec)`.
- `on_close(peer, code)`: runs before the engine releases resources.
- `tracks=[...]`: outgoing tracks. `ice_servers` overrides the router's ICE servers.

`Peer` sends with `send_message`, `send_track` and `remove_track`, which
renegotiate. `peer.channel(spec)` and `peer.send_message(spec, message)` are
typed by the spec's `receive_model` and `send_model`; a channel name gives an
untyped handle. Reliability options such as `ordered` are keyword-only.

`receive_model=Never` or `send_model=Never` declares a one-way channel. The
server drops and reports messages it does not accept, `send_message` raises
`WebRtcError(invalid_message)`, and `on_message` handlers or `async for` on a
channel without inbound messages are refused. The generated client omits
`send` or `subscribe` for that direction. `Never` is not inferred as a type
argument; annotate the spec, for example `DataChannelSpec[Never, Tick]`. It also provides `stats()`, `close(code)` and `wait_closed()`.
The session stops tracks it owns when it closes. It never stops tracks passed
with `owned=False`.

Set `role=Role.OFFERER` when the server should create the offer, for example
when the browser only watches.

## Browser client

```console
canon webrtc contract --app app:app --output contract.json
canon webrtc generate --contract contract.json --output src/rtc --client-name PracticeRtcClient
```

```ts
import { PracticeRtcClient, isRtcError } from "./rtc/index.js";

const client = new PracticeRtcClient({ baseUrl: "https://api.example.com" });
const microphone = await navigator.mediaDevices.getUserMedia({ audio: true });

const session = await client.practice.connect({
  lessonId: "greetings", // path parameter
  token: "demo", // query parameter
  tracks: microphone, // borrowed: never stopped by the session
  timeoutMs: 10_000,
  onTrack: ({ track }) => {
    audio.srcObject = new MediaStream([track]);
  },
  onMessage: { messages: (feedback) => console.log(feedback.words) },
});

await session.channels.messages.send({ text: "hello" }); // typed as Prompt
await session.close(); // idempotent
```

`timeoutMs` limits connection setup, including asynchronous credential providers,
and stops applying once the session is ready. Use `signal` for cancellation of
the session's entire lifetime: aborting it also closes an already running audio
or video session. An `AbortSignal.timeout(...)` therefore ends the session when
its timer expires, even if connection setup already succeeded.

Later renegotiations, including `addTrack()` and `removeTrack()`, use the
server's `negotiation_timeout_ms` and end the session with `negotiation_timeout`
if they cannot complete. Channel sends wait at most `sendTimeoutMs` (10 seconds
by default) for opening or buffer drainage before ending with `slow_consumer`.
Permanent peer-connection failure rejects pending operations with `transport_lost`.

Failures are `RtcError`s with a stable `code`, such as `session_limit`,
`negotiation_timeout`, `media_rejected` or `transport_lost`. Browsers do not
expose the HTTP status of a refused WebSocket handshake. A session denied by a
dependency therefore fails with `connection_failed`. The full list of codes is
in [the protocol](docs/protocol.md#close-codes).

Both commands accept `--check`, which fails on drift without writing.
Generated directories contain a manifest. Stale generated files are removed;
files you add yourself are kept.

Configure shared credentials with `new PracticeRtcClient({ query: async () =>
({ token: await refreshToken() }) })`. The provider runs for every connection.
Query values are merged in this order, with later sources taking precedence:
the `baseUrl` query string, `client.config.query`, `connect({ query: ... })`, then
named connect parameters such as `connect({ lessonId: "greetings", token: "new" })`.
Use wire aliases as keys inside query maps. An omitted named parameter preserves
shared values; `null` explicitly removes a query value. Required query parameters
are checked after merging. Generated query fields are optional so shared
credentials can supply them; required path parameters must still be passed.

## Security

Session admission is ordinary FastAPI dependency code, so authorization stays in
your application. Two WebSocket properties need attention:

- **Origins.** CORS does not apply to WebSocket handshakes, and browsers attach
  cookies to cross-site handshakes. If admission relies on cookies, pass
  `WebRtcRouter(allowed_origins=["https://app.example.com"])`. Handshakes from
  other origins are refused with HTTP 403 before the session factory runs.
  Requests without an `Origin` header come from non-browser clients and are not
  restricted by this check.
- **Query tokens.** Browsers cannot set headers on a WebSocket, so tokens travel
  in the URL. URLs can appear in proxy and server access logs. Prefer short-lived,
  single-use session tickets issued by an authenticated HTTP endpoint over
  long-lived access tokens, and supply them through the client's `query` provider.

## Limits and ICE

`SessionLimits` bounds negotiation time, pending candidates, signaling and
message sizes, inbound queues and send buffers. It also sets grace periods for
transport and signaling loss. Every limit has a categorized error.
`TurnRestCredentials` issues time-limited TURN REST credentials per user. It can
give the server-side engine internal relay URLs.

## Documentation

- [Signaling protocol](docs/protocol.md)
- [Canon composition and Dishka session/message scopes](docs/canon-dishka.md)
- [Remote send-only engines and per-viewer leases](examples/remote_video/README.md)
- [Optional browser video health observation](docs/media-health.md)
- [Typed HTTP admission and short-lived tickets](docs/admission-tickets.md)
- [Changelog](CHANGELOG.md)
- Runnable example: [`examples/practice`](examples/practice). Its generated
  client is in `examples/practice/client`.

The engine interface abstracts signaling and lifecycle. `MediaKind`,
`SignalingState`, `ConnectionState`, `TransceiverDirection` and `ChannelState`
are public `StrEnum` classes available from `fastapi_canon.integrations.webrtc` and
`fastapi_canon.integrations.webrtc.engine`. Custom adapters return enum members and convert native
strings with constructors such as `ConnectionState(native_state)`. Use
`MediaKind.VIDEO` for video stats and codec-preference keys, and
`TransceiverDirection.RECVONLY` when adding a receive-only transceiver.
Their string and JSON values match the WebRTC names. Construct engine flags with
`EngineCapabilities(trickle_ice=False, ice_restart=False, renegotiation=True)`;
all flags are keyword-only and `negotiated_data_channels` defaults to `True`.
`ChannelParameters` keeps its positional label and requires keywords for its
options, for example `ChannelParameters("chat", ordered=False)`.
`MessageFormat` and `SessionEventKind` are public enums too; use
`MessageFormat.JSON` and `SessionEventKind.TRACK` in descriptors and event hooks.
SDP descriptions use `DescriptionType.OFFER` or `DescriptionType.ANSWER` for their
`type` field. Protocol message discriminators retain their individual literal types.

Native outgoing tracks and received frames remain engine-specific: aiortc uses
`MediaStreamTrack` and PyAV frames. Its statistics return `jitter_seconds=None`
because the public report does not identify the per-stream codec clock rate
needed for conversion.

Generated TypeScript models provide compile-time message types; the browser
runtime checks formats and sizes but does not enforce every JSON Schema rule.
Server-side Pydantic adapters validate incoming and outgoing typed messages.
