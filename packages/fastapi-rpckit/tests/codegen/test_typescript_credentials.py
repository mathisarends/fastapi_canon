import subprocess
from textwrap import dedent

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_async_credentials_refresh_rejection_budget_and_close(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "credentials.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {GreetingClient, WebSocketTransport, RpcConnectError}
          = require("./compiled/generated");
        const timers = new Map(); let serial = 0;
        globalThis.setTimeout = (callback, delay) => {
          const id = ++serial; timers.set(id, {callback, delay}); return id;
        };
        globalThis.clearTimeout = id => timers.delete(id);
        async function until(condition) {
          for (let i = 0; i < 100; i++) {
            if (condition()) return; await Promise.resolve();
          }
          throw new Error("State did not progress");
        }
        function fireBackoff() {
          const found = [...timers].find(([, t]) => t.delay === 0);
          assert(found); timers.delete(found[0]); found[1].callback();
        }
        class Socket {
          readyState = 1; listeners = new Map();
          addEventListener(type, callback) { this.listeners.set(type, callback); }
          send(frame) {
            const message = JSON.parse(frame);
            if (message.id !== undefined)
              this.listeners.get("message")({data: JSON.stringify({
                jsonrpc: "2.0", id: message.id, result: {text: "ok"},
              })});
          }
          close() { this.readyState = 3; }
          expire(reason = "credentials_expired") {
            this.listeners.get("close")({code: 1008, reason});
          }
        }
        async function main() {
          for (const policy of ["refresh", "denied", "generic", "opaque"]) {
            let providers = 0, attempts = 0;
            const sockets = [], received = [], states = [];
            const client = await GreetingClient.connect({
              lazy: true, reconnect: true, requestTimeoutMs: null,
              heartbeatIntervalMs: null, reconnectRandom: () => 0,
              onStateChange: (_, state, error) => states.push({state, error}),
              credentials: async (endpoint, signal) => {
                providers++; assert(signal instanceof AbortSignal);
                const url = new URL(endpoint.url);
                url.searchParams.set("ticket", `credential-${providers}`);
                return {url, subprotocols: ["rpc.v1", `auth.ticket-${providers}`]};
              },
              socketFactory: (url, protocols) => {
                attempts++; received.push([String(url), protocols]);
                if (attempts > 1 && (policy === "denied" || policy === "opaque")) {
                  const error = new Error(`Sensitive URL ${url}`);
                  if (policy === "denied") error.status = 401;
                  throw error;
                }
                const socket = new Socket(); sockets.push(socket); return socket;
              },
            });
            await client.greeting.say({name: "first"});
            sockets[0].expire(policy === "generic" ? "forbidden" : undefined);
            if (policy === "refresh") {
              await until(() => sockets.length === 2 && client.state === "connected");
              assert.equal(providers, 2);
              assert(received[1][0].includes("ticket=credential-2"));
              assert.deepEqual(received[1][1], ["rpc.v1", "auth.ticket-2"]);
              assert(states.some(s => s.state === "reconnecting"));
              assert.equal((await client.greeting.say({name: "after"})).text, "ok");
            } else if (policy === "opaque") {
              for (let retry = 0; retry < 2; retry++) {
                await until(() => [...timers.values()].some(t => t.delay === 0));
                fireBackoff();
              }
              const error = await client.closed;
              assert(error instanceof RpcConnectError);
              assert.equal(attempts, 4); // Initial + three recovery attempts.
              assert.equal(providers, 4);
              assert(!error.message.includes("credential"));
            } else {
              const error = await client.closed;
              assert.equal(providers, policy === "denied" ? 2 : 1);
              if (policy === "denied") {
                assert(error instanceof RpcConnectError);
                assert(error.permanent); assert.equal(error.status, 401);
                assert(!error.message.includes("credential"));
              }
            }
            await client.close(); assert.equal(timers.size, 0);
          }

          let entered = false, release, signal, factories = 0;
          const opening = WebSocketTransport.open("ws://test", {
            credentials: async (_, current) => {
              entered = true; signal = current;
              return await new Promise(resolve => { release = resolve; });
            },
            signal: (globalThis.controller = new AbortController()).signal,
            socketFactory: () => { factories++; return new Socket(); },
          }).catch(e => e);
          await until(() => entered);
          controller.abort("stop refresh"); release({url: "ws://test/new"});
          assert.equal(await opening, "stop refresh");
          assert(signal.aborted); assert.equal(factories, 0);
          assert.equal(timers.size, 0);
          console.log("credentials complete");
        }
        main().catch(e => {console.error(e); process.exitCode = 1;});
    """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "credentials complete" in result.stdout
