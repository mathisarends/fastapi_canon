import asyncio
import json
import shutil
import subprocess
from copy import deepcopy
from textwrap import dedent

import pytest
from fastapi_canon.integrations.websockets import RpcChannel, RpcDisconnect, RpcService
from fastapi_canon.integrations.websockets.codegen import generate_typescript_client
from fastapi_canon.integrations.websockets.codegen.typescript import (
    TypeScriptClientOptions,
)
from fastapi_canon.integrations.websockets.testing import InMemorySocketFactory


@pytest.fixture(scope="module")
def cancellation_sdk(document, tmp_path_factory):
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    node = shutil.which("node.exe") or shutil.which("node")
    if npx is None or node is None:
        pytest.skip("Node.js and npx are required")
    root = tmp_path_factory.mktemp("typescript_cancellation")
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
        root / "generated",
        TypeScriptClientOptions(
            with_transport="websocket", client_name="GreetingClient"
        ),
    )
    (root / "types.ts").write_text(
        dedent("""
        import { GreetingClient, type RpcRequestOptions } from "./generated";
        import { RpcClientCore, defineRoute } from "./generated/core";
        const oldTransport = {
          async request(_method: string, _params?: object): Promise<unknown> {
            return {};
          },
          async *notifications() {},
          async close() {},
        };
        const client = new GreetingClient(oldTransport);
        const options: RpcRequestOptions = { signal: new AbortController().signal };
        void client.greeting.say({ name: "Ada" }, options);
        void client.greeting.greeted(options);
        void client.greeting.say({ name: "Ada" });
        void client.greeting.greeted();
        const core = new RpcClientCore(oldTransport);
        void core.request(
          defineRoute<undefined, string>({ method: "ping" }),
          undefined,
          options,
        );
        // @ts-expect-error signals are AbortSignal instances
        void client.greeting.greeted({ signal: "invalid" });
        // @ts-expect-error required method params are still required
        void client.greeting.say(options);
    """),
        encoding="utf-8",
    )
    checked = subprocess.run(
        [
            npx,
            "--yes",
            "--package",
            "typescript",
            "tsc",
            "--strict",
            "--target",
            "ES2021",
            "--module",
            "Node16",
            "--moduleResolution",
            "Node16",
            "--lib",
            "ES2021,DOM,ESNext.Disposable",
            "--skipLibCheck",
            "--outDir",
            str(root / "compiled"),
            str(root / "types.ts"),
        ],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    return root, node


def test_abort_timeout_response_and_disconnect_races(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "races.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          WebSocketTransport,
          RpcRequestTimeoutError,
          RpcConnectionLostError,
          GreetingClient,
        } = require("./compiled/generated");
        const { RpcClientCore, defineRoute } = require("./compiled/generated/core");
        const timers = new Map();
        let nextTimer = 1;
        globalThis.setTimeout = (callback) => {
          const id = nextTimer++;
          timers.set(id, callback);
          return id;
        };
        globalThis.clearTimeout = (id) => timers.delete(id);
        function trackedSignal() {
          const controller = new AbortController();
          const signal = controller.signal;
          const listeners = new Set();
          const add = signal.addEventListener.bind(signal);
          const remove = signal.removeEventListener.bind(signal);
          signal.addEventListener = (type, fn, options) => {
            if (type === "abort") listeners.add(fn);
            add(type, fn, options);
          };
          signal.removeEventListener = (type, fn, options) => {
            if (type === "abort") listeners.delete(fn);
            remove(type, fn, options);
          };
          return { controller, signal, listeners };
        }
        class Socket {
          readyState = 1;
          sent = [];
          listeners = new Map();
          addEventListener(type, fn) {
            const listeners = this.listeners.get(type) ?? [];
            listeners.push(fn);
            this.listeners.set(type, listeners);
          }
          send(data) {
            const frame = JSON.parse(data);
            this.sent.push(frame);
            this.onSend?.(frame);
          }
          close() {
            this.readyState = 3;
          }
          emit(type, event) {
            for (const fn of this.listeners.get(type) ?? []) fn(event);
          }
          reply(id, result) {
            this.emit("message", {
              data: JSON.stringify({ jsonrpc: "2.0", id, result }),
            });
          }
        }
        async function open() {
          const socket = new Socket();
          const transport = await WebSocketTransport.open("ws://test", {
            socketFactory: () => socket,
          });
          return { socket, transport };
        }
        function clean(tracker) {
          assert.equal(tracker.listeners.size, 0, "abort listeners remain");
          assert.equal(timers.size, 0, "timers remain");
        }
        async function main() {
          for (const ending of [
            "abort",
            "timeout",
            "response",
            "disconnect",
            "close",
          ]) {
            const { socket, transport } = await open();
            const tracker = trackedSignal();
            const reason = { user: "cancelled" };
            const promise = transport
              .request("work", undefined, { signal: tracker.signal })
              .catch((e) => e);
            const staleTimer = [...timers.values()][0];
            if (ending === "abort") tracker.controller.abort(reason);
            if (ending === "timeout") staleTimer();
            if (ending === "response") socket.reply(1, "result");
            if (ending === "disconnect")
              socket.emit("close", { code: 1011, reason: "lost" });
            if (ending === "close") await transport.close();
            const outcome = await promise;
            tracker.controller.abort(reason);
            staleTimer(); // Simulate a timer callback already dequeued before cleanup.
            if (ending === "abort") assert.equal(outcome, reason);
            if (ending === "timeout")
              assert.ok(outcome instanceof RpcRequestTimeoutError);
            if (ending === "response") assert.equal(outcome, "result");
            if (ending === "disconnect" || ending === "close")
              assert.ok(outcome instanceof RpcConnectionLostError);
            const controls = socket.sent.filter(
              (frame) => frame.method === "$/cancelRequest",
            );
            assert.equal(
              controls.length,
              ending === "abort" || ending === "timeout" ? 1 : 0,
            );
            if (controls.length)
              assert.deepEqual(controls[0], {
                jsonrpc: "2.0",
                method: "$/cancelRequest",
                params: { id: 1 },
              });
            clean(tracker);
            if (socket.readyState === 1) {
              socket.emit("message", {
                data: JSON.stringify({
                  jsonrpc: "2.0",
                  id: 1,
                  error: {
                    code: -32800,
                    message: "Request cancelled",
                    data: { code: "request_cancelled" },
                  },
                }),
              });
              const next = transport.request("next");
              socket.reply(2, "alive");
              assert.equal(await next, "alive");
            }
            await transport.close();
          }
          for (const preAbort of [true, false]) {
            const { socket, transport } = await open();
            const tracker = trackedSignal();
            if (preAbort) tracker.controller.abort();
            const params = {
              toJSON() {
                tracker.controller.abort();
                return {};
              },
            };
            const failure = await transport
              .request("unsent", params, { signal: tracker.signal })
              .catch((e) => e);
            assert.equal(failure, tracker.signal.reason);
            assert.equal(failure.name, "AbortError");
            assert.equal(socket.sent.length, 0);
            clean(tracker);
            await transport.close();
          }
          const during = await open();
          const sending = trackedSignal();
          during.socket.onSend = (frame) => {
            if (frame.method === "work") sending.controller.abort("during send");
          };
          assert.equal(
            await during.transport
              .request("work", undefined, { signal: sending.signal })
              .catch((e) => e),
            "during send",
          );
          assert.equal(during.socket.sent.length, 2);
          assert.equal(during.socket.sent[1].method, "$/cancelRequest");
          clean(sending);
          await during.transport.close();

          const failure = await open();
          const failing = trackedSignal();
          failure.socket.onSend = (frame) => {
            if (frame.method === "$/cancelRequest")
              throw new Error("control send failed");
          };
          const call = failure.transport
            .request("work", undefined, { signal: failing.signal })
            .catch((e) => e);
          failing.controller.abort("original reason");
          assert.equal(await call, "original reason");
          const healthy = failure.transport.request("healthy");
          failure.socket.reply(2, "ok");
          assert.equal(await healthy, "ok");
          clean(failing);
          await failure.transport.close();

          const old = await open();
          const original = trackedSignal();
          const lost = old.transport
            .request("work", undefined, { signal: original.signal })
            .catch((e) => e);
          await old.transport.close();
          assert.ok((await lost) instanceof RpcConnectionLostError);
          const replacement = await open();
          const reused = replacement.transport.request("work");
          original.controller.abort();
          replacement.socket.reply(1, "new generation");
          assert.equal(await reused, "new generation");
          assert.equal(old.socket.sent.length, 1);
          assert.equal(replacement.socket.sent.length, 1);
          clean(original);
          await replacement.transport.close();

          for (const method of ["say", "greeted"]) {
            const { socket, transport } = await open();
            const client = new GreetingClient(transport);
            const tracker = trackedSignal();
            const pending = (
              method === "say"
                ? client.greeting.say({ name: "Ada" }, { signal: tracker.signal })
                : client.greeting.greeted({ signal: tracker.signal })
            ).catch((e) => e);
            while (socket.sent.length === 0) await Promise.resolve();
            tracker.controller.abort("API abort");
            assert.equal(await pending, "API abort");
            assert.equal(socket.sent[1].method, "$/cancelRequest");
            clean(tracker);
            await client.close();
          }
          for (const phase of ["before", "middleware", "connect"]) {
            const tracker = trackedSignal();
            let opened = 0,
              lateReject;
            const source = {
              requestTimeoutMs: 100,
              get() {
                opened++;
                return new Promise((_, reject) => {
                  lateReject = reject;
                });
              },
              async close() {},
            };
            const core = new RpcClientCore(source, {
              middleware:
                phase === "middleware"
                  ? [
                      {
                        request() {
                          return new Promise((_, reject) => {
                            lateReject = reject;
                          });
                        },
                      },
                    ]
                  : [],
            });
            if (phase === "before") tracker.controller.abort("before opening");
            const pending = core
              .request(defineRoute({ method: "work" }), undefined, {
                signal: tracker.signal,
              })
              .catch((e) => e);
            tracker.controller.abort("stop waiting");
            assert.equal(await pending, tracker.signal.reason);
            assert.equal(opened, phase === "connect" ? 1 : 0);
            lateReject?.(new Error("late failure"));
            await Promise.resolve();
            clean(tracker);
            await core.close();
          }
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
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_typescript_abort_and_timeout_cancel_a_real_server(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "roundtrip.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const readline = require("node:readline");
        const {
          WebSocketTransport,
          RpcRequestTimeoutError,
        } = require("./compiled/generated");
        const input = readline.createInterface({ input: process.stdin });
        let socket, entered, stopped;
        input.on("line", (line) => {
          const message = JSON.parse(line);
          if (message.kind === "entered") entered();
          if (message.kind === "stopped") stopped();
          if (message.kind === "frame") socket.receive(message.frame);
        });
        function output(message) {
          process.stdout.write(JSON.stringify(message) + "\\n");
        }
        class Socket {
          readyState = 1;
          listeners = new Map();
          constructor() {
            output({ kind: "open" });
          }
          addEventListener(type, fn) {
            const listeners = this.listeners.get(type) ?? [];
            listeners.push(fn);
            this.listeners.set(type, listeners);
          }
          send(frame) {
            output({ kind: "frame", frame });
          }
          close() {
            this.readyState = 3;
            output({ kind: "close" });
          }
          receive(frame) {
            for (const fn of this.listeners.get("message") ?? []) fn({ data: frame });
          }
        }
        async function main() {
          const timers = new Map();
          let timerId = 0;
          globalThis.setTimeout = (fn) => {
            timers.set(++timerId, fn);
            return timerId;
          };
          globalThis.clearTimeout = (id) => timers.delete(id);
          for (const ending of ["abort", "timeout"]) {
            socket = new Socket();
            const started = new Promise((resolve) => {
              entered = resolve;
            });
            const cancelled = new Promise((resolve) => {
              stopped = resolve;
            });
            const transport = await WebSocketTransport.open("ws://test/rpc", {
              socketFactory: () => socket,
            });
            const controller = new AbortController();
            const pending = transport
              .request("cancellation.work", undefined, { signal: controller.signal })
              .catch((e) => e);
            await started;
            if (ending === "abort") controller.abort("user cancelled");
            else [...timers.values()][0]();
            const failure = await pending;
            if (ending === "abort") assert.equal(failure, "user cancelled");
            else assert.ok(failure instanceof RpcRequestTimeoutError);
            await cancelled;
            assert.equal(await transport.request("cancellation.ping"), "pong");
            assert.equal(timers.size, 0);
            await transport.close();
          }
          input.close();
          process.stdin.pause();
        }
        main().catch((error) => {
          console.error(error);
          process.exitCode = 1;
          input.close();
          process.stdin.pause();
        });
    """),
        encoding="utf-8",
    )
    entered, stopped = asyncio.Event(), asyncio.Event()
    cancellations = []
    channel = RpcChannel("cancellation")

    @channel.method()
    async def work() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.set()
            raise

    @channel.method()
    async def ping() -> str:
        return "pong"

    service = RpcService()
    service.socket("/rpc", channels=[channel])
    before = asyncio.all_tasks()
    process = await asyncio.create_subprocess_exec(
        node,
        "--unhandled-rejections=strict",
        str(runner),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    relays = []

    async def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def relay(socket):
        try:
            while True:
                await send({"kind": "frame", "frame": await socket.recv()})
        except RpcDisconnect:
            return

    try:
        async with InMemorySocketFactory(service) as factory, asyncio.timeout(10):
            socket = None
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message["kind"] == "open":
                    entered.clear()
                    stopped.clear()
                    socket = await factory("ws://test/rpc")
                    relays.append(asyncio.create_task(relay(socket)))
                elif message["kind"] == "close":
                    await socket.close()
                else:
                    frame = json.loads(message["frame"])
                    await socket.send(message["frame"])
                    if frame["method"] == "cancellation.work":
                        await entered.wait()
                        await send({"kind": "entered"})
                    elif frame["method"] == "$/cancelRequest":
                        cancellations.append(frame)
                        await stopped.wait()
                        await send({"kind": "stopped"})
            await process.wait()
            error = (await process.stderr.read()).decode()
            assert process.returncode == 0, error
            assert (
                cancellations
                == [
                    {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}}
                ]
                * 2
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in relays:
            task.cancel()
        await asyncio.gather(*relays, return_exceptions=True)
    assert not asyncio.all_tasks() - before
