import shutil
import subprocess
from textwrap import dedent

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_middleware_public_types_are_strictly_checked(cancellation_sdk):
    root, _ = cancellation_sdk
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    consumer = root / "middleware_types.ts"
    consumer.write_text(
        dedent("""
        import {GreetingClient, type RpcClientMiddleware,
          type RpcClientCall, type RpcClientCallOutcome,
          RpcResponseValidationError} from "./generated";
        // @ts-expect-error obsolete hook types are not exported
        import type { RpcClientHook } from "./generated";
        // @ts-expect-error connection loss has one current error type
        import { RpcConnectionClosed } from "./generated";
        const middleware: RpcClientMiddleware = {
          async request(call: RpcClientCall, next: () => Promise<unknown>) {
            const id: string = call.callId;
            const attempt: number = call.attempt;
            call.metadata.traceparent = id;
            // @ts-expect-error metadata values must be strings
            call.metadata.baggage = 42;
            call.params = {name: id + attempt};
            try {await next();} finally {
              const outcome: RpcClientCallOutcome | undefined = call.outcome;
              const duration: number | undefined = call.duration;
              if (outcome && "error" in outcome) void outcome.error;
              void duration;
            }
          },
        };
        void GreetingClient.connect({middleware: [middleware]});
        // @ts-expect-error obsolete client hooks are removed
        void GreetingClient.connect({hooks: []});
        // @ts-expect-error lifecycle state replaces onDisconnect
        void GreetingClient.connect({onDisconnect: () => {}});
        void new RpcResponseValidationError("work");
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
            "--noEmit",
            str(consumer),
        ],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_client_middleware_outcomes_retry_abort_and_cleanup(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "middleware.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {RpcClientCore, defineRoute, defineNotification,
          RpcRemoteError, RpcConnectionLostError, RpcConnectError,
          RpcResponseValidationError, RpcTransportError}
          = require("./compiled/generated/core");
        const timers = new Map(); let serial = 0;
        globalThis.setTimeout = (callback, delay) => {
          const id = ++serial; timers.set(id, {callback, delay}); return id;
        };
        globalThis.clearTimeout = id => timers.delete(id);
        async function settle() {
          for (let i = 0; i < 60; i++) await Promise.resolve();
        }
        function record(name, events, calls, cleanup) {
          return {async request(call, next) {
            const local = {}; events.push(`${name}:enter`); calls.push(call);
            try { await next(); }
            finally {
              assert(local); assert(call.outcome); events.push(`${name}:exit`);
              if (cleanup !== undefined) throw cleanup;
            }
          }};
        }
        function source(request, acquireError) {
          const transport = {request, async *notifications() {}, async close() {}};
          return {get: async () => {
            if (acquireError !== undefined) throw acquireError;
            return transport;
          }, async close() {}, retryDelay: () => 0};
        }
        async function main() {
          for (const mode of ["success", "remote", "transport", "validation",
            "acquisition", "serialization"]) {
            const calls = [], events = [];
            const failure = mode === "remote"
              ? new RpcRemoteError(-32000, "domain", "Domain failure")
              : mode === "acquisition" ? new RpcConnectError("Denied", true)
              : new RpcConnectionLostError(true);
            const rpc = new RpcClientCore(source(async () => {
              if (mode === "remote" || mode === "transport") throw failure;
              return "ok";
            }, mode === "acquisition" ? failure : undefined), {
              middleware: [record("outer", events, calls),
                record("inner", events, calls)],
            });
            const route = defineRoute({method: "work", server: "primary",
              validateResult: value => {
                if (mode === "validation") throw new Error("invalid");
                return value;
              }});
            const params = {}; if (mode === "serialization") params.big = 1n;
            const result = await rpc.request(route, params).catch(e => e);
            assert.deepEqual(events, ["outer:enter", "inner:enter",
              "inner:exit", "outer:exit"]);
            assert.equal(calls[0], calls[1]);
            assert.equal(calls[0].attempt, 1); assert(calls[0].callId);
            assert.equal(calls[0].server, "primary");
            assert(calls[0].duration >= 0);
            if (mode === "success") assert.equal(calls[0].outcome.result, "ok");
            else {
              assert.equal(calls[0].outcome.error, result);
              if (mode === "validation")
                assert(result instanceof RpcResponseValidationError);
              else if (mode === "serialization")
                assert(result instanceof RpcTransportError);
              else assert.equal(result, failure);
            }
            await rpc.close(); assert.equal(timers.size, 0);
          }
          {
            const calls = [], events = [];
            const rpc = new RpcClientCore(source(async () => "wire"), {
              middleware: [record("results", events, calls)]
            });
            const result = await rpc.request(defineRoute({method: "converted",
              validateResult: raw => ({validated: raw})}));
            assert.equal(calls[0].outcome.result, result);
            assert.deepEqual(result, {validated: "wire"});
            await rpc.close();
          }
          let sent = 0; const calls = [], events = [];
          const rpc = new RpcClientCore(source(async () => {
            if (++sent === 1) throw new RpcConnectionLostError(false);
            return "ok";
          }), {retryAttempts: 1, middleware: [record("metrics", events, calls)],
          });
          const task = rpc.request(defineRoute({method: "work"}));
          await settle(); const timer = [...timers][0];
          assert.equal(timer[1].delay, 0);
          timers.delete(timer[0]); timer[1].callback();
          assert.equal(await task, "ok");
          assert.deepEqual(calls.map(c => c.attempt), [1, 2]);
          assert.equal(calls[0].callId, calls[1].callId);
          await rpc.close(); assert.equal(timers.size, 0);

          for (const failed of [true, false]) {
            const calls = [], events = [];
            const original = new RpcConnectionLostError(true);
            const cleanup = new RpcConnectionLostError(false);
            let sends = 0;
            const rpc = new RpcClientCore(source(async () => {
              sends++; if (failed) throw original; return "ok";
            }), {retryAttempts: 3, middleware: [record("outer", events, calls),
              record("inner", events, calls, cleanup)]});
            const result = await rpc.request(defineRoute({method: "work"}))
              .catch(e => e);
            assert.equal(result, failed ? original : cleanup);
            assert.equal(calls[0].outcome.error, result); assert.equal(sends, 1);
            await rpc.close(); assert.equal(timers.size, 0);
          }
          const cancelCalls = [], cancelEvents = [], controller = new AbortController();
          const reason = new Error("caller abort");
          const cancelled = new RpcClientCore({get: () => new Promise(() => {}),
            async close() {}}, {
              middleware: [record("metrics", cancelEvents, cancelCalls)]});
          const pending = cancelled.request(defineRoute({method: "work"}),
            undefined, {signal: controller.signal}).catch(e => e);
          await settle(); controller.abort(reason);
          assert.equal(await pending, reason); await settle();
          assert.equal(cancelCalls[0].outcome.error, reason);
          assert.equal(cancelCalls[0].transport, undefined);
          await cancelled.close(); assert.equal(timers.size, 0);

          const controls = [], controlEvents = [], queue = [];
          let waiting;
          const transport = {async request(method, params) {
            if (method.endsWith(".subscribe")) {
              const value = {jsonrpc: "2.0", method: "updates",
                params: {subscriptionId: "s", payload: {value: 1}}};
              if (waiting) {const resolve = waiting; waiting = undefined;
                resolve({value, done: false});} else queue.push(value);
              return {subscriptionId: "s"};
            }
          }, notifications(signal) {return {[Symbol.asyncIterator]() {return {
            next() {if (queue.length) return Promise.resolve({value: queue.shift(),
              done: false}); return new Promise(resolve => {waiting = resolve;
              signal?.addEventListener("abort", () => resolve({done: true}),
                {once: true});});}, return: async () => ({done: true}),
          };}};}, async close() {}};
          const subscribed = new RpcClientCore(transport, {
            middleware: [record("metrics", controlEvents, controls)]});
          const stream = subscribed.subscribeRemote(
            defineNotification({method: "updates"}))[Symbol.asyncIterator]();
          assert.deepEqual((await stream.next()).value, {value: 1});
          await stream.return();
          assert.deepEqual(controls.map(c => c.route.method),
            ["updates.subscribe", "updates.unsubscribe"]);
          assert(controls.every(c => "result" in c.outcome));
          await subscribed.close(); assert.equal(timers.size, 0);
          console.log("middleware complete");
        }
        main().catch(e => {console.error(e); process.exitCode = 1;});
    """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "middleware complete" in result.stdout
