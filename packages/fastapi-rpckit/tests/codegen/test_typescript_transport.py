import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from fastapi_canon.integrations.websockets.codegen import generate_typescript_client
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)


def test_transport_resilience_in_node(document: dict[str, Any], tmp_path: Path) -> None:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    deployed = deepcopy(document)
    deployed["servers"] = [
        {
            "name": "rpc",
            "url": "ws://test",
            "x-rpckit-transport": {"type": "websocket", "messageEncoding": "json"},
        }
    ]
    generate_typescript_client(
        deployed,
        tmp_path / "generated",
        TypeScriptClientOptions(with_transport="websocket"),
    )
    runner = tmp_path / "transport.test.ts"
    runner.write_text(
        dedent(
            """
            import { WebSocketTransport } from "./generated/transport";
            import {
              RpcClientError, RpcConnectError, RpcConnectionLostError,
              RpcRequestTimeoutError, RpcTransportError,
              type WebSocketFactory,
            } from "./generated";
            import { RpcClientCore, defineRoute } from "./generated/core";
            const browserFactory: WebSocketFactory = url => new WebSocket(url);
            void browserFactory;

            function assert(value: unknown, message: string): asserts value {
              if (!value) throw new Error(message);
            }

            class Socket {
              readyState = 1;
              sendError?: Error;
              sent: Record<string, unknown>[] = [];
              listeners = new Map<string, ((event: { data: unknown }) => void)[]>();
              addEventListener(
                type: string, listener: (event: { data: unknown }) => void,
              ) {
                const listeners = this.listeners.get(type) ?? [];
                this.listeners.set(type, [...listeners, listener]);
              }
              send(data: string) {
                this.sent.push(JSON.parse(data));
                if (this.sendError) throw this.sendError;
              }
              close() { this.readyState = 3; }
              receive(message: object) {
                for (const listener of this.listeners.get("message") ?? []) {
                  listener({ data: JSON.stringify({ jsonrpc: "2.0", ...message }) });
                }
              }
              emit(type: string, event: unknown) {
                for (const listener of this.listeners.get(type) ?? []) {
                  listener(event as { data: unknown });
                }
              }
            }

            async function main() {
              const set = globalThis.setTimeout;
              const clear = globalThis.clearTimeout;
              const timers = new Map<number, () => void>();
              let next = 1;
              globalThis.setTimeout = ((callback: () => void) => {
                const id = next++;
                timers.set(id, callback);
                return id;
              }) as typeof setTimeout;
              globalThis.clearTimeout = ((id: number) => {
                timers.delete(id);
              }) as typeof clearTimeout;
              try {
                const socket = new Socket();
                const transport = await WebSocketTransport.open("ws://test", {
                  socketFactory: () => socket,
                });
                const slow = transport.request("slow").then(
                  () => { throw new Error("slow call unexpectedly succeeded"); },
                  error => {
                    assert(error instanceof RpcRequestTimeoutError, "untyped timeout");
                    assert(error instanceof RpcTransportError, "wrong timeout base");
                    assert(error.method === "slow", "timeout lost method");
                    assert(error.timeout === 30_000, "timeout lost duration");
                  },
                );
                const [timer, expire] = [...timers.entries()][0];
                timers.delete(timer);
                expire();
                await slow;
                const other = transport.request("other");
                socket.receive({ id: 1, result: "late" });
                socket.receive({ id: 999, error: { code: -1, message: "unknown" } });
                socket.receive({ id: 2, result: "ok" });
                socket.receive({ id: 2, result: "duplicate" });
                assert(await other === "ok", "late response killed another call");
                assert(socket.readyState === 1, "socket closed on late response");

                socket.receive({ id: "server:1", method: "room.ping" });
                const answer = socket.sent.at(-1)!;
                assert(answer.id === "server:1", "wrong server request id");
                assert(JSON.stringify(answer.error) === JSON.stringify({
                  code: -32601,
                  message: "Method not found",
                  data: { code: "method_not_found", details: { method: "room.ping" } },
                }), "wrong server-request error");
                const later = transport.request("later");
                socket.receive({ id: 3, result: "alive" });
                assert(await later === "alive", "server request killed transport");
                await transport.close();
                assert(await transport.closed === undefined, "normal close failed");
                assert(timers.size === 0, "timers leaked after close");

                const lostSocket = new Socket();
                const lost = await WebSocketTransport.open("ws://test", {
                  socketFactory: () => lostSocket,
                });
                const notifications = lost.notifications()[Symbol.asyncIterator]();
                const notification = notifications.next().catch(error => error);
                const sent = lost.request("sent").catch(error => error);
                const close = { code: 1011, reason: "server failure" };
                lostSocket.emit("close", close);
                const unsent = lost.request("unsent").catch(error => error);
                const failures = await Promise.all([sent, unsent]);
                for (const failure of failures) {
                  assert(failure instanceof RpcConnectionLostError, "untyped loss");
                  assert(failure instanceof RpcClientError, "wrong loss base");
                  assert(failure.code === 1011, "close code lost");
                  assert(failure.reason === "server failure", "reason lost");
                  assert(failure.cause === close, "original close cause lost");
                }
                assert(failures[0] instanceof RpcConnectionLostError,
                  "sent failure untyped");
                assert(failures[1] instanceof RpcConnectionLostError,
                  "unsent failure untyped");
                assert(failures[0].sent === true, "sent call marked unsent");
                assert(failures[1].sent === false, "unsent call marked sent");
                assert(await notification instanceof RpcConnectionLostError,
                  "notification lost its error type");
                assert(await lost.closed instanceof RpcConnectionLostError,
                  "closed lost its error type");

                const sendSocket = new Socket();
                const original = new Error("failed after delivery");
                sendSocket.sendError = original;
                const sending = await WebSocketTransport.open("ws://test", {
                  socketFactory: () => sendSocket,
                });
                const failure = await sending.request("delivered").catch(e => e);
                assert(failure instanceof RpcConnectionLostError, "raw send failure");
                assert(failure.sent === true, "partial send marked unsent");
                assert(failure.cause === original, "send cause lost");
                assert(sendSocket.sent.length === 1, "test frame wasn't delivered");

                for (const status of [401, 403, 404, 503]) {
                  const cause = Object.assign(new Error("rejected"), {
                    response: { statusCode: status },
                  });
                  const rejection = await WebSocketTransport.open("ws://test", {
                    socketFactory: () => { throw cause; },
                  }).catch(e => e);
                  assert(rejection instanceof RpcConnectError, "raw connect failure");
                  assert(rejection.permanent === (status !== 503), "wrong permanence");
                  assert(rejection.status === status, "handshake status lost");
                  assert(rejection.cause === cause, "handshake cause lost");
                }
                const opaqueSocket = new Socket();
                opaqueSocket.readyState = 0;
                const opaque = WebSocketTransport.open("ws://test", {
                  socketFactory: () => opaqueSocket,
                }).catch(e => e);
                opaqueSocket.emit("error", {});
                const hidden = await opaque;
                assert(hidden instanceof RpcConnectError, "opaque failure untyped");
                assert(hidden.status === undefined, "invented browser status");
                assert(hidden.permanent === false, "opaque failure permanent");

                const policySocket = new Socket();
                policySocket.readyState = 0;
                const policy = WebSocketTransport.open("ws://test", {
                  socketFactory: () => policySocket,
                }).catch(e => e);
                policySocket.emit("close", { code: 1008, reason: "policy" });
                const refused = await policy;
                assert(refused instanceof RpcConnectError, "policy failure untyped");
                assert(refused.code === 1008 && refused.permanent, "wrong policy");

                const hookedSocket = new Socket();
                const hookedTransport = await WebSocketTransport.open("ws://test", {
                  socketFactory: () => hookedSocket,
                });
                const circular: Record<string, unknown> = {};
                circular.self = circular;
                const invalidParams = await hookedTransport.request(
                  "invalid", circular,
                ).catch(e => e);
                assert(invalidParams instanceof RpcTransportError,
                  "serialization error untyped");
                assert(hookedSocket.readyState === 1, "serialization closed socket");
                const applicationError = new Error("application middleware failed");
                const core = new RpcClientCore(hookedTransport, { middleware: [{
                  request() { throw applicationError; },
                }] });
                const hookFailure = await core.request(
                  defineRoute<undefined, unknown>({ method: "hooked" }),
                ).catch(e => e);
                assert(hookFailure === applicationError, "middleware error wrapped");
                assert(hookedSocket.sent.length === 0, "failed middleware sent a call");
                await core.close();
                assert(timers.size === 0, "taxonomy tests leaked timers");

                for (const invalid of [
                  { id: "bad", result: 1 },
                  { id: true, result: 1 },
                  { id: 999 },
                  { id: 999, result: 1, error: { code: -1, message: "bad" } },
                  { id: 999, error: "bad" },
                  { id: 999, error: { code: "bad", message: "bad" } },
                ]) {
                  const badSocket = new Socket();
                  const bad = await WebSocketTransport.open("ws://test", {
                    socketFactory: () => badSocket,
                  });
                  const call = bad.request("pending").then(
                    () => { throw new Error("malformed response accepted"); },
                    error => assert(error instanceof Error, "wrong protocol failure"),
                  );
                  badSocket.receive(invalid);
                  await call;
                  assert(badSocket.readyState === 3, "malformed response kept open");
                  assert(timers.size === 0, "failure leaked a request timer");
                }
              } finally {
                globalThis.setTimeout = set;
                globalThis.clearTimeout = clear;
              }
            }
            void main();
            """
        ),
        encoding="utf-8",
    )
    compiled = tmp_path / "compiled"
    checked = subprocess.run(
        [
            npx,
            "--yes",
            "--package",
            "typescript",
            "tsc",
            "--strict",
            "--target",
            "ES2022",
            "--module",
            "Node16",
            "--moduleResolution",
            "Node16",
            "--lib",
            "ES2022,DOM,ESNext.Disposable",
            "--skipLibCheck",
            "--outDir",
            str(compiled),
            str(runner),
        ],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    result = subprocess.run(
        [node, str(compiled / "transport.test.js")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
