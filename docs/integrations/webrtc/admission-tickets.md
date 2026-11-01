# Typed HTTP admission and short-lived signaling tickets

Browser WebSocket APIs do not expose a refused handshake's HTTP problem body.
`admission_errors` documents those failures but cannot change that browser API.
Use an optional authenticated HTTP admission step when the UI needs typed denial
details before opening signaling. Ordinary cookie-based WebSocket admission
remains valid when that distinction is unnecessary.

## Application flow

1. The browser calls an authenticated HTTP endpoint such as
   `POST /browser/{browser_id}/video-ticket`.
2. That endpoint checks account/login ownership, feature switches and availability
   in a short unit of work. Canon `raises` documents the existing application
   errors, so an HTTP client can read their Problem Details.
3. On success, return an opaque, random, short-lived ticket with `Cache-Control:
   no-store`. Bind it to the user/login, browser, intended operation and expiry.
4. Supply it through the generated WebRTC client's query parameters when opening
   the existing session endpoint.
5. A session dependency validates and atomically consumes it, checks the current
   authorization state, and only then acquires the viewer lease. After acceptance,
   failures use WebRTC protocol errors and cleanup.

The ticket store, authentication, user identifiers and authorization service are
application-owned. They are not new `fastapi-webrtc` APIs. In a multi-worker
deployment the store must support atomic consume-and-expiry across workers.
Minting a ticket does not reserve remote capacity unless the application explicitly
models a reservation lease and releases expired or abandoned reservations.

```ts
// `issueVideoTicket` is the application's typed HTTP client operation.
const { ticket } = await issueVideoTicket(browserId);
const session = await rtc.video.connect({ browserId, query: { ticket } });
```

Do not treat a separate availability preflight as a guarantee that a later
WebSocket will succeed. Ownership, login expiry and remote capacity can change
between calls. The final authorization and lease acquisition must remain atomic
at their owning service boundary. A consumed ticket should not be reused on a
retry; request a fresh one.

## Transport details

Validate allowed browser origins on both endpoints according to the application's
cookie/CSRF policy. Short-lived tickets complement the existing WebSocket origin
checks. Bound ticket expiry by login/session expiry; do not issue long-lived access
tokens as query values. Redact ticket query parameters in application and proxy
logs and avoid returning internal connection URLs.

Declare the same named Canon errors on the HTTP route and optional
`CanonWebRtcRouter` session. Expected admission failures remain HTTP problems;
accepted-session failures retain WebRTC error framing. The ticket endpoint makes
its own HTTP response readable, while a subsequent failed WebSocket handshake can
still appear as `connection_failed` to the browser.
