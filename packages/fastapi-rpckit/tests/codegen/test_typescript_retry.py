import subprocess
from textwrap import dedent

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_retry_delivery_deadlines_cancellation_and_ids(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "retry.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          RpcClientCore, defineRoute, RpcConnectionLostError,
          RpcRequestTimeoutError, RpcConnectError, RpcRemoteError,
        } = require("./compiled/generated/core");
        const {WebSocketTransport} = require("./compiled/generated");
        const timers = new Map(); let serial = 0;
        globalThis.setTimeout = (callback, delay) => {
          const id = ++serial; timers.set(id, {callback, delay}); return id;
        };
        globalThis.clearTimeout = id => timers.delete(id);
        async function settle() {
          for (let i = 0; i < 40; i++) await Promise.resolve();
        }
        function fire(delay) {
          const found = [...timers].find(([, t]) => t.delay === delay);
          assert(found, `Missing timer ${delay}`);
          timers.delete(found[0]); found[1].callback();
        }
        function source(request, budget) {
          return {
            get: async () => ({request, async *notifications() {},
              async close() {}}), async close() {},
            retryDelay: () => 0, requestTimeoutMs: budget,
          };
        }
        async function main() {
          for (const sent of [false, true]) {
            for (const idempotent of [false, true]) {
              let calls = 0;
              const error = new RpcConnectionLostError(sent);
              const rpc = new RpcClientCore(source(async () => {
                if (++calls === 1) throw error; return 42;
              }), {retryAttempts: 1});
              const task = rpc.request(defineRoute({method: "work", idempotent}))
                .catch(e => e);
              await settle();
              const retries = !sent || idempotent;
              if (retries) fire(0);
              assert.equal(await task, retries ? 42 : error);
              assert.equal(calls, retries ? 2 : 1);
              assert.equal(timers.size, 0); await rpc.close();
            }
          }
          for (const failure of [new RpcConnectError("denied", true),
            new RpcRemoteError(-32000, "domain", "Domain failure"),
            new Error("validation"), new RpcRequestTimeoutError("write", 1)]) {
            let calls = 0;
            const rpc = new RpcClientCore(source(async () => {
              calls++; throw failure;
            }), {retryAttempts: 5});
            assert.equal(await rpc.request(defineRoute({method: "write"}))
              .catch(e => e), failure);
            assert.equal(calls, 1); await rpc.close();
          }
          const disabled = new RpcClientCore(source(async () => {
            throw new RpcConnectionLostError(false);
          }));
          assert(await disabled.request(defineRoute({method: "work"}))
            .catch(e => e) instanceof RpcConnectionLostError);
          assert.equal(timers.size, 0); await disabled.close();

          for (const finish of ["deadline", "abort", "close"]) {
            let calls = 0;
            const rpc = new RpcClientCore(source(async () => {
              calls++; throw new RpcConnectionLostError(false);
            }, 100), {retryAttempts: 5});
            const controller = new AbortController(), reason = new Error("cancel");
            const task = rpc.request(defineRoute({method: "work"}),
              undefined, {signal: controller.signal}).catch(e => e);
            await settle(); assert([...timers.values()].some(t => t.delay === 0));
            if (finish === "deadline") fire(100);
            else if (finish === "abort") controller.abort(reason);
            else await rpc.close();
            const error = await task;
            if (finish === "deadline") assert(error instanceof RpcRequestTimeoutError);
            else if (finish === "abort") assert.equal(error, reason);
            else assert(error instanceof RpcConnectionLostError);
            await settle(); assert.equal(calls, 1); assert.equal(timers.size, 0);
            await rpc.close();
          }

          class Socket {
            readyState = 1; frames = []; listeners = new Map();
            addEventListener(type, callback) { this.listeners.set(type, callback); }
            send(frame) {
              const message = JSON.parse(frame); this.frames.push(message);
              if (message.method === "read" && message.id === 2)
                this.listeners.get("message")({data: JSON.stringify({
                  jsonrpc: "2.0", id: message.id, result: 42,
                })});
            }
            close() { this.readyState = 3; }
          }
          const socket = new Socket();
          const transport = await WebSocketTransport.open("ws://test", {
            socketFactory: () => socket, requestTimeoutMs: null,
          });
          const rpc = new RpcClientCore({get: async () => transport,
            close: () => transport.close(), retryDelay: () => 0,
            requestTimeoutMs: 100}, {retryAttempts: 1, retryAttemptTimeoutMs: 10});
          const task = rpc.request(defineRoute({method: "read", idempotent: true}));
          await settle(); fire(10); await settle(); fire(0);
          assert.equal(await task, 42);
          assert.deepEqual(socket.frames.filter(f => f.method === "read")
            .map(f => f.id), [1, 2]);
          assert(socket.frames.some(f => f.method === "$/cancelRequest"));
          assert.equal(timers.size, 0); await rpc.close();
          console.log("retry complete");
        }
        main().catch(e => {console.error(e); process.exitCode = 1;});
        """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "retry complete" in result.stdout
