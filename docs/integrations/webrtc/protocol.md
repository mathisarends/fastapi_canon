# Signaling protocol `fastapi-webrtc.v1`

Each session uses one WebSocket at its declared path. The browser offers the
subprotocol `fastapi-webrtc.v1`. A connection that offers only other
subprotocols is accepted and then closed with `unsupported_protocol`. All
messages are JSON text frames with a `type` field. Receivers ignore unknown
fields. An unknown `type` is a `protocol_error`.

## Admission

The session factory and its dependencies run before the WebSocket is accepted.
An `HTTPException` refuses the handshake with its HTTP status (for example, 403).
This requires Starlette >=0.41, which the package declares explicitly. Browsers
expose a refused handshake as `connection_failed`, without the HTTP status.
No engine resources exist at that point. The router and session `max_sessions`
limits are checked next. Exceeding them closes the socket with
`session_limit`. Generator dependencies stay open until the session has ended.

## Server messages

| Type | Payload | Meaning |
| --- | --- | --- |
| `session` | `protocol`, `session_id`, `name`, `role`, `ice_servers`, `media`, `channels`, `limits`, `capabilities` | First message; the peer exists. |
| `description` | `description: {type, sdp}`, `negotiation` | Offer or answer. |
| `candidate` | `candidate: RTCIceCandidateInit \| null` | Trickled candidate; `null` ends the candidates. |
| `ready` | | Transport connected, channels open and `on_start` finished. |
| `error` | `code`, `message`, `channel?` | Non-terminal failure, such as a rejected message. |

## Client messages

| Type | Payload |
| --- | --- |
| `description` | `description: {type, sdp}`, `negotiation` |
| `candidate` | `candidate: RTCIceCandidateInit \| null` |

Every offer carries an increasing `negotiation` number. An answer echoes the
number of the offer it answers. The receiver discards stale answers.
Renegotiation follows the perfect-negotiation pattern: the browser is polite
and the server impolite. When offers collide, the browser rolls back and answers
the server's offer.

With the default `answerer` role, the browser sends the first offer. With
`offerer`, the server sends it right after `session`. Candidates that arrive
before their remote description are buffered up to `max_pending_candidates`.
Candidates for an outdated ICE username fragment are dropped.

## Data channels

Channels with `negotiated: true` (the default) use the fixed `id` from the
contract. Both sides create them, so no in-band announcement is needed. In-band
channels (`negotiated: false`) are created by the offering side. The server closes
undeclared remote channels and reports a non-terminal `channel_rejected`
error; the session continues.

`json` channels carry JSON text that is validated against the channel model.
`text` carries strings, `binary` carries bytes, and `raw` carries both
unchanged. `none` marks a one-way channel: nothing is accepted in that
direction, and the receiving side reports `invalid_message` for every message. Messages larger than `max_message_bytes` are rejected. Invalid
inbound messages produce a non-terminal `error` with the channel name; the
offending value is never echoed.

## Close codes

The close reason is the error code. Codes map to WebSocket close codes:

| Error code | Close code |
| --- | --- |
| `closed` | 1000 |
| `shutdown` | 1001 |
| `unsupported_protocol` | 1002 |
| `protocol_error`, `media_rejected`, `channel_rejected` | 1008 |
| `message_too_large` | 1009 |
| `engine_failure`, `handler_failure` | 1011 |
| `session_limit` | 1013 |
| every other session failure | 4000 |

The other session failures are `negotiation_failed`, `negotiation_timeout`,
`media_timeout`, `transport_lost`, `signaling_lost`, `queue_exhausted`,
`slow_consumer`, `invalid_message` and `channel_closed`. The browser client
adds `connection_failed` (the WebSocket could not be opened, including refused
admission) and `aborted` (the caller's `AbortSignal`).

## Teardown

Every accepted session closes signaling, cancels its tasks, runs `on_close`
with the code, stops owned tracks and closes the engine peer, then releases
its slot and dependency scopes. `on_close` and engine cleanup each keep at
least one second, even when cancelled handlers used up
`shutdown_timeout_seconds`. A signaling loss shorter than
`signaling_loss_grace_seconds` and a
transport interruption shorter than `transport_loss_grace_seconds` do not end
the session. `WebRtcRouter.aclose()` runs at application shutdown and closes
every session with `shutdown`.
