# JSON-RPC protocol

RPC socket endpoints exchange JSON-RPC 2.0 messages. Methods accept named
parameters, and request IDs may be strings or integers. Calls include an `id` and
receive a result or structured error; notifications omit `id` and receive no
response. Batches receive one array containing the responses for their calls.
See [Services](services.md) for message, batch, and pending-work limits.

## Request cancellation

To stop an admitted call on the same socket, send this reserved notification:

```json
{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":42}}
```

It has no top-level `id`. Its params contain exactly one `id`, whose type is
integer or string; booleans, null, arrays, and extra parameters are invalid.
Malformed control notifications are ignored and never cancel work. A control
sent as a call receives `invalid_request`. User method names cannot use the `$/`
prefix.

Cancellation bypasses handler concurrency, pending admission, and response
collector capacity. It can stop an executing or queued call even while the writer
or outgoing queue is blocked. Unknown, completed, and admission-rejected IDs are
ignored. Notifications and explicit null request IDs have no cancellable ID.
Integer `42` and string `"42"` identify different requests.

The original call receives this error after its dependency and middleware scopes
exit; the cancel notification itself receives no response:

```json
{"jsonrpc":"2.0","id":42,"error":{"code":-32800,"message":"Request cancelled","data":{"code":"request_cancelled","details":null}}}
```

The error is a built-in `RpcRequestCancelledError`, including with strict error
declarations. Cancellation races with completion: an already completed call can
still return its result. Within a batch, controls are processed in wire order;
cancelling a preceding item keeps its failure in the original batch response.
Clients that already abandoned the call should ignore its valid late response.
The generated WebSocket SDKs send the control automatically after local timeout
or caller cancellation; see [Client cancellation](clients.md#cancel-a-request).

Duplicate in-flight IDs of the same type close the connection with
`PROTOCOL_ERROR` (1002) instead of replacing the original task or sending an
ambiguous response. Reuse an ID only after its invocation has finished. Connection
loss and server shutdown cancel tasks and release capacity without generating
`request_cancelled` responses. Outer serving-task cancellation remains observable
as `asyncio.CancelledError`.

OpenRPC exports `x-rpc-request-cancellation` with the notification method, params
schema, and error code. Explicit method budgets appear as
`x-rpc-request-timeout` in seconds; null disables an inherited serving budget.
See [Request deadlines](services.md#request-deadlines) for cooperative cancellation
and subscription handshake semantics. This wire control is implemented by RPC
socket serving; a custom transport built on `RpcServer.handle()` owns its registry
and cancellation routing.

## Request metadata

The optional top-level `meta` extension carries bounded W3C tracing strings.
OpenRPC advertises `x-rpc-request-metadata`; params remain unchanged, including
no-argument requests. Metadata is independent per batch member and is never an
authorization source. See [Per-request trace context](observability.md#per-request-trace-context)
for the exact capability, validation, interoperability, and SDK recipes.
