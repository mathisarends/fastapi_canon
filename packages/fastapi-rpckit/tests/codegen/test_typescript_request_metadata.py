import json
import subprocess
from copy import deepcopy
from textwrap import dedent

from fastapi_canon.integrations.websockets import RpcChannel
from fastapi_canon.integrations.websockets.adapters.opentelemetry import OpenTelemetry
from fastapi_canon.integrations.websockets.codegen import render_typescript_client
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


async def test_typescript_middleware_wire_metadata_server_parent_and_controls(
    cancellation_sdk, document
):
    root, node = cancellation_sdk
    runner = root / "metadata.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          RpcClientCore,
          defineRoute,
          defineNotification,
          validateRequestMetadata,
        } = require("./compiled/generated/core");
        const {
          GreetingClient,
          WebSocketTransport,
        } = require("./compiled/generated");
        const traceparent = "00-11111111111111111111111111111111-2222222222222222-01";
        const requests = [];
        const timers = new Map();
        let nextTimer = 1;
        globalThis.setTimeout = (fn) => {
          const id = nextTimer++;
          timers.set(id, fn);
          return id;
        };
        globalThis.clearTimeout = (id) => timers.delete(id);
        class Socket {
          readyState = 1;
          listeners = new Map();
          addEventListener(type, fn) {
            const list = this.listeners.get(type) ?? [];
            list.push(fn);
            this.listeners.set(type, list);
          }
          receive(message) {
            for (const fn of this.listeners.get("message") ?? [])
              fn({ data: JSON.stringify(message) });
          }
          send(frame) {
            const request = JSON.parse(frame);
            if ("method" in request) {
              requests.push(request);
              queueMicrotask(() =>
                this.receive({
                  jsonrpc: "2.0",
                  id: request.id,
                  result: { names: [] },
                }),
              );
            } else this.lastResponse = request;
          }
          close() {
            this.readyState = 3;
          }
        }
        const middleware = {
          async request(call, next) {
            call.metadata.traceparent = traceparent;
            call.metadata.tracestate = "vendor=value";
            await next();
          },
        };
        async function main() {
          const socket = new Socket();
          const transport = await WebSocketTransport.open("ws://test", {
            socketFactory: () => socket,
          });
          const client = new GreetingClient(transport, { middleware: [middleware] });
          await client.greeting.greeted();
          assert.equal(requests[0].meta.traceparent, traceparent);
          assert.ok(!("params" in requests[0]));
          const old = new RpcClientCore(transport, {
            middleware: [middleware],
            retryAttempts: 2,
          });
          await assert.rejects(
            old.request(defineRoute({ method: "old" })),
            /contract does not support/,
          );
          assert.equal(requests.length, 1);
          const clean = new RpcClientCore(transport, { closeTransport: false });
          await clean.request(defineRoute({ method: "old" }));
          assert.ok(!("meta" in requests[1]));
          await clean.close();
          for (const metadata of [
            null,
            [],
            { other: "x" },
            { traceparent: 1 },
            { baggage: "x".repeat(4096) },
            { baggage: "é".repeat(2048) },
            { baggage: "\\ud800" },
          ]) {
            assert.throws(() => validateRequestMetadata(metadata));
            await assert.rejects(
              transport.request("invalid", undefined, { metadata }),
            );
            await assert.rejects(client.greeting.greeted({ metadata }));
          }
          assert.equal(requests.length, 2);
          assert.equal(
            validateRequestMetadata({ baggage: "x".repeat(4082) }).baggage.length,
            4082,
          );
          socket.receive({
            jsonrpc: "2.0",
            id: "reverse",
            method: "reverse",
            meta: { traceparent },
          });
          assert.equal(socket.lastResponse.error.code, -32601);
          socket.receive({
            jsonrpc: "2.0",
            id: "bad",
            method: "reverse",
            meta: null,
          });
          assert.equal(socket.lastResponse.error.code, -32600);

          const controls = [];
          let deliver;
          const adapter = {
            async request(method, params, options) {
              controls.push({ method, metadata: options.metadata });
              if (method.endsWith(".subscribe")) {
                queueMicrotask(() =>
                  deliver({
                    jsonrpc: "2.0",
                    method: "updates",
                    params: { subscriptionId: "s", payload: "ready" },
                  }),
                );
                return { subscriptionId: "s" };
              }
              return null;
            },
            async *notifications() {
              yield await new Promise((resolve) => {
                deliver = resolve;
              });
            },
            async close() {},
          };
          const core = new RpcClientCore(adapter, {
            middleware: [middleware],
            supportsMetadata: true,
          });
          const stream = core
            .subscribeRemote(defineNotification({ method: "updates" }))
            [Symbol.asyncIterator]();
          assert.equal((await stream.next()).value, "ready");
          await stream.return();
          assert.deepEqual(
            controls.map((x) => x.method),
            ["updates.subscribe", "updates.unsubscribe"],
          );
          assert.ok(controls.every((x) => x.metadata.traceparent === traceparent));
          await core.close();
          await client.close();
          assert.equal(timers.size, 0);
          console.log(JSON.stringify(requests[0]));
          console.log("metadata complete");
        }
        main().catch((error) => {
          console.error(error);
          process.exitCode = 1;
        });
    """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", str(runner)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "metadata complete" in result.stdout
    request = json.loads(result.stdout.splitlines()[0])
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    channel = RpcChannel("greeting")

    @channel.method("greeted")
    async def greeted() -> dict[str, list[str]]:
        return {"names": []}

    server = channel.create_server(
        middleware=[OpenTelemetry(tracer_provider=provider, metrics=False)]
    )
    assert (await server.handle(request)).result == {"names": []}
    span = exporter.get_finished_spans()[0]
    assert span.context.trace_id == int("1" * 32, 16)
    assert span.parent.span_id == int("2" * 16, 16) and span.parent.is_remote
    provider.shutdown()
    old = deepcopy(document)
    old.pop("x-rpc-request-metadata")
    files = render_typescript_client(old, TypeScriptClientOptions())
    assert "supportsMetadata: false" in files["client.ts"]
