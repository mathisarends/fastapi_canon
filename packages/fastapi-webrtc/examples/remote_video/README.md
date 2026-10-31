# Remote send-only video

This executable adapter example keeps an existing media peer and its direct media
path. Implement `VideoService.subscribe()` and `VideoLease` against your remote
service, then call `create_app(service)`. No GStreamer, Canon or Dishka dependency
is required. The regression suite scripts only this native media boundary.

The session binds `RemoteVideoEngine(lease)` through `Session.engine`; a router
engine is optional when each factory supplies one. Router engines remain the
default for sessions without an override. The remote peer already owns its video
sender, so no artificial local track is needed to identify the remote stream.
Native track descriptors remain appropriate when they represent actual tracks.

## Ownership and cancellation

Authenticate and check ownership before acquiring media resources. Keep database
transactions short. Then acquire a separate viewer lease in an async context
manager or a `yield` dependency. Its `finally` must close only that viewer, even
when router capacity is exhausted or the connection is cancelled before an offer.
Never stop the shared browser, capture process or another viewer's subscription.

The runtime closes peers it creates, and dependency teardown closes leases even
when no peer exists. `VideoLease.aclose()` must therefore be idempotent. This also
covers partial failures during `open()`: the dependency still owns its lease.
`on_close` alone is insufficient because refused sessions do not invoke it.
An unsuccessful subscribe operation must release any partial allocation itself.

The router's session limit is local to that router/process, and is checked after
factory resolution. Enforce exclusive viewer ownership atomically in the remote
service when multiple workers or processes share a stream. Do not use a separate
"is free" check followed by an unprotected allocation.

## Engine behavior and diagnostics

Forward native connection-state events through `PeerObserver`. Sending an answer
does not establish the connection. Call observers on the creating event loop;
threaded engines can use `loop.call_soon_threadsafe`.

The example deliberately rejects restart, answering offers, data channels and
local-track operations. It advertises its actual capabilities. Implement another
adapter when those operations are needed rather than silently pretending success.

Partial statistics report known connection state and leave RTP streams absent.
Absent collections mean unknown coverage, not proof of no traffic. The initial
server media deadline watches incoming media; it does not diagnose video decoded
or displayed by a receiving browser. A remote stats bridge should normalize units
into `ConnectionStats` and include only metrics the backend actually measures.

Map expected native failures to `WebRtcError` with an existing stable category and
a safe message inside the adapter. Peer creation preserves such categories;
unexpected failures become `engine_failure`. A busy viewer can be refused during
admission with an application error. After acceptance it must use the WebRTC error
protocol. New public busy/unavailable wire categories need a separate protocol
decision; do not put credentials, SDP, native error text or remote URLs in messages.

The tests cover separate bindings, second-viewer rejection, disconnect, shutdown,
partial setup failure and cancellation before an offer. They do not validate a
deployed GStreamer service, TURN/NAT or a proxy installation.
