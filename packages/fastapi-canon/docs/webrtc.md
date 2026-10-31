# WebRTC admission errors

Install `fastapi-canon[webrtc]` to declare Canon error contracts on WebRTC
sessions. A plain `WebRtcRouter` still works in `Feature.routers`; use
`CanonWebRtcRouter` when admission needs the same declarative errors as HTTP.

```python
from fastapi import FastAPI
from fastapi_canon import Composition, Error, ErrorOptions, ErrorRegistry, Feature
from fastapi_canon.integrations.webrtc import CanonWebRtcRouter, Session
from fastapi_canon.integrations.webrtc.contract import build_contract
from fastapi_canon.integrations.webrtc.engines.aiortc import AiortcEngine


class BrowserMissing(Exception):
    pass


BROWSER_MISSING = Error(
    BrowserMissing, status=404, code="browser_missing", title="Browser missing"
)
errors = ErrorRegistry(errors=[BROWSER_MISSING])
rtc = CanonWebRtcRouter(engine=AiortcEngine(), error_registry=errors)


@rtc.session("/browser/{browser_id}/video", raises=[BROWSER_MISSING])
async def video(browser_id: str) -> Session:
    # Authenticate and load the stream through application services here.
    raise BrowserMissing()


app = Composition(
    Feature(name="browser", routers=[rtc]),
    errors=ErrorOptions(type_base="https://example.test/problems"),
).apply(FastAPI())
contract = build_contract(app)
```

The engine extra remains independent: install `fastapi-canon[webrtc,aiortc]` for this
example, or provide your own engine. Canon core imports neither WebRTC nor aiortc.

Router-level `raises` and session-level `raises` combine and deduplicate by the
identity of the existing `Error` objects. Definitions outside the router's registry
fail during configuration. `Feature` discovers the registry automatically, as it
does for `CanonRouter`. When mounting through an ordinary parent `APIRouter`, pass
the registry explicitly as `Feature(errors=errors)`.

Declare failures reachable from authentication and other FastAPI dependencies
explicitly; the library does not infer which exceptions arbitrary code can raise.
`raises` documents failures and does not restrict runtime exception mapping.

Export with `build_contract(app)` after composition so errors inherit the composed
registry's type URI base. Exporting routes alone requires a locally resolved error
registry. Each session's `admission_errors` contains status, code, type, title,
description, media type and a self-contained Problem Details JSON Schema. Generated
session descriptors retain this as `admissionErrors`. It describes HTTP rejection
before WebSocket acceptance, independently of HTTP OpenAPI operations.

The composed registry maps application exceptions from dependencies or the session
factory before acceptance. No engine peer is allocated for these denials. After
acceptance, failures use WebRTC protocol errors and teardown; they cannot send an
HTTP problem response. Browser WebSocket APIs do not expose failed handshake
response bodies. Contract metadata therefore does not create typed browser access
to those responses. A separate HTTP admission flow is described in the
[WebRTC integration guide](../../fastapi-webrtc/docs/canon-dishka.md).
