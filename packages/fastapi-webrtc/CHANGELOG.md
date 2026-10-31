# Changelog

## 0.3.0 - 2026-10-24

[GitHub release and artifacts](https://github.com/mathisarends/canon/releases/tag/fastapi-webrtc-v0.3.0)
and [PyPI](https://pypi.org/project/fastapi-webrtc/0.3.0/).

### Breaking changes

- Replace the engine's `MediaKind`, `SignalingState`, `ConnectionState`,
  `TransceiverDirection` and `ChannelState` literal aliases with public `StrEnum`
  classes, also exported from `fastapi_webrtc`. Use members such as
  `MediaKind.AUDIO` and `ConnectionState.CONNECTED` in engine implementations,
  callbacks, stats and codec-preference maps. Convert native strings with the
  corresponding enum constructor; aiortc does this at its adapter boundary.
  Values and string/JSON serialization stay unchanged.
- Make `EngineCapabilities` keyword-only. Replace positional flags with
  `EngineCapabilities(trickle_ice=..., ice_restart=..., renegotiation=...)`;
  `negotiated_data_channels` still defaults to `True`.
- Make `ChannelParameters` options after `label` keyword-only. For example,
  replace `ChannelParameters("chat", False)` with
  `ChannelParameters("chat", ordered=False)`.
- Replace `MessageFormat` and `SessionEventKind` literal aliases with public
  `StrEnum` classes, also exported from `fastapi_webrtc`. Use members such as
  `MessageFormat.JSON` and `SessionEventKind.TRACK` when constructing descriptors
  and events. Signaling descriptors reuse `Direction`, `Role` and `MessageFormat`;
  parsed SDP sections expose `TransceiverDirection`. JSON values stay unchanged.
- Use the public `DescriptionType` enum for `SessionDescription.type` and signaling
  payloads. Replace `SessionDescription("offer", sdp)` with
  `SessionDescription(DescriptionType.OFFER, sdp)` (likewise for `ANSWER`).
  The aiortc adapter converts native description types directly instead of
  treating any non-offer value as an answer.
- Declare remaining type aliases with Python 3.12's `type` syntax. These exported
  names are `typing.TypeAliasType` instances; introspection tools must handle
  their lazy `__value__` instead of expecting a runtime union or `Callable`.
  `SessionEvent.media_kind` now uses `MediaKind` too.

## 0.2.0 - 2026-10-20

### Breaking changes

- Generated runtimes export `observeVideoHealth` and its types. Regenerate clients
  and rename any colliding session/client declarations reported by code generation.
  Existing `connect()` readiness semantics are unchanged.
- Add `Session.engine` and allow a router without a default engine when factories
  bind one per session. Custom constructor overrides should accept an optional
  engine. Supply either a router engine or a session engine; missing bindings are
  refused with `handler_failure` and returned tracks are cleaned up.
- Extend `WebRtcRouter.session()` and `SessionDeclaration` with the optional
  `admission_errors` contract-provider hook. Custom router overrides must accept
  and forward this keyword. Regenerate clients to include declared admission
  metadata in session descriptors. The signaling protocol remains v1.

### Added

- Optional browser video health observation with separate opt-in first-frame and
  ongoing-stall deadlines, recovery events, per-track decoded stats and optional
  compositor frame callbacks. Abort, session close and `stop()` release observers;
  observation never closes sessions, stops tracks or chooses a fallback UI.
- A tested remote send-only adapter example with per-viewer async-context-manager
  leases, explicit unsupported operations and partial statistics. Categorized
  `WebRtcError` failures during peer creation retain their stable protocol code.
- Canon-independent admission contract export, including optional generated
  descriptor metadata. HTTP denials remain distinct from accepted-session errors;
  this metadata does not expose failed WebSocket handshake bodies in browsers.
- An optional [HTTP admission ticket recipe](docs/admission-tickets.md) for readable
  denials before signaling, with application-owned authorization and atomic ticket
  consumption. It does not reserve remote viewer capacity automatically.

## 0.1.0 - 2026-10-20

Initial release.

- `WebRtcRouter.session()` declares WebRTC sessions as WebSocket signaling
  endpoints. FastAPI dependencies admit sessions before any peer is created and
  stay open until cleanup finished, also when the server cancels the endpoint.
- `Session` handlers: `on_start`, `run`, `on_track`, typed `on_message`,
  `on_close` and outgoing tracks. `Peer` sends messages and tracks with
  renegotiation, and exposes stats and close codes. Stats report jitter as
  `None` when the engine cannot convert it to seconds (aiortc). `on_close` runs
  once per accepted session and keeps at least one second even when cancelled
  handlers used up `shutdown_timeout_seconds`.
- Track ownership: session tracks and `send_track(track)` are stopped on removal
  or cleanup, including rejected and partially started sessions;
  `send_track(track, owned=False)` keeps shared sources running behind a
  sender-local forwarding track. Only active
  incoming tracks count against `MediaPolicy.max_incoming_tracks`.
- Typed data channels from Pydantic models, `str` or `bytes`. JSON messages use
  the contract's serialization aliases. Channels support negotiated IDs,
  validated keyword-only reliability options, size limits, bounded inbound
  queues and send backpressure. A `DataChannelSpec` is generic over its message
  types: `on_message` accepts specs as keys, and `peer.channel(spec)` and
  `peer.send_message(spec, ...)` are type-checked against its models.
  `Never` as `receive_model` or `send_model` declares a one-way channel
  (format `none`); the generated client omits `send` or `subscribe` for it.
- Signaling protocol `fastapi-webrtc.v1`. It uses perfect negotiation,
  candidate buffering, ICE username checks, categorized close codes and
  negotiation, media and transport deadlines. Negotiation timeouts and shutdown
  cancel blocked ICE and engine startup.
- ICE server providers, `TurnRestCredentials` with separate engine URLs and
  session event hooks.
- `WebRtcRouter(allowed_origins=...)` refuses cross-site browser handshakes with
  HTTP 403 before the session factory runs.
- A pluggable `Engine` protocol, with an `aiortc` reference engine behind the
  `aiortc` extra.
- Tested Canon/Dishka composition with explicit async-factory injection,
  session services, per-message request scopes and ordered cleanup.
- `fastapi-webrtc contract` and `fastapi-webrtc generate` export a session
  contract and generate a dependency-free TypeScript browser client with typed
  parameters, channels and errors. Query models become flat named parameters.
  Ambiguous generated names fail before files are written. Both commands support
  `--check` drift detection.
- The browser client merges query values from the base URL, shared credential
  providers, per-call maps and named parameters, in that order; `null` removes
  a value. `timeoutMs` bounds credential resolution and connection setup;
  `signal` cancels the whole session. Renegotiation
  and channel sends (`sendTimeoutMs`) have deadlines after connect, and
  permanent transport failure rejects pending operations.
