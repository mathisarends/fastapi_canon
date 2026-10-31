import subprocess
from textwrap import dedent

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_reconnect_policy_state_and_cleanup_in_node(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "reconnect.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          RpcTransportPool,
          RpcClientCore,
          RpcConnectError,
          RpcConnectionLostError,
          RpcRequestTimeoutError,
          defineRoute,
        } = require("./compiled/generated/core");
        const {
          WebSocketTransport,
          GreetingClient,
        } = require("./compiled/generated");
        const timers = new Map();
        let next = 0;
        globalThis.setTimeout = (callback, delay) => {
          const id = ++next;
          timers.set(id, { callback, delay });
          return id;
        };
        globalThis.clearTimeout = (id) => timers.delete(id);
        async function until(condition) {
          for (let i = 0; i < 100; i++) {
            if (condition()) return;
            await Promise.resolve();
          }
          throw new Error("State did not progress");
        }
        function deferred() {
          let resolve, reject;
          const promise = new Promise((yes, no) => {
            resolve = yes;
            reject = no;
          });
          return { promise, resolve, reject };
        }
        class Transport {
          closed = new Promise((resolve) => {
            this.disconnect = resolve;
          });
          closedByClient = false;
          async request() {
            return "ok";
          }
          async *notifications() {}
          async close() {
            this.closedByClient = true;
            this.disconnect();
          }
        }
        function pool(open, extra = {}) {
          return new RpcTransportPool({
            endpoints: [{ server: "a", url: "ws://test" }],
            requestTimeoutMs: null,
            open,
            ...extra,
          });
        }
        async function main() {
          const expiryConnections = [];
          const expiring = pool(async () => {
            const transport = new Transport();
            expiryConnections.push(transport);
            return transport;
          }, { reconnect: true });
          await expiring.get("a");
          expiryConnections[0].disconnect(
            new RpcConnectionLostError(false, 1008, "credentials_expired"),
          );
          await until(
            () => expiryConnections.length === 2 && expiring.state === "connected",
          );
          await expiring.close();
          for (const permanent of [true, false]) {
            const error = new RpcConnectError("initial failure", permanent);
            let calls = 0;
            const source = pool(
              async () => {
                calls++;
                throw error;
              },
              { reconnect: true },
            );
            assert.equal(await source.get("a").catch((e) => e), error);
            assert.equal(await source.closed, error);
            assert.equal(calls, 1);
            await source.close();
          }
          for (const code of [1001, 1012, 1013, 1002, 1003, 1007, 1008, 1009]) {
            const connections = [],
              states = [];
            const source = pool(
              async () => {
                const transport = new Transport();
                connections.push(transport);
                return transport;
              },
              { reconnect: true, onStateChange: (...event) => states.push(event) },
            );
            await source.get("a");
            let terminal = false;
            source.closed.then(() => {
              terminal = true;
            });
            connections[0].disconnect(
              new RpcConnectionLostError(false, code, "drop"),
            );
            await until(() =>
              states.some(
                (event) => event[1] === "reconnecting" || event[1] === "closed",
              ),
            );
            if ([1001, 1012, 1013].includes(code)) {
              await until(
                () => connections.length === 2 && source.state === "connected",
              );
              assert.equal(terminal, false);
              assert.deepEqual(
                states.map((e) => e[1]),
                ["connecting", "connected", "reconnecting", "connected"],
              );
            } else {
              assert.equal(
                (await source.closed) instanceof RpcConnectionLostError,
                true,
              );
              assert.equal(connections.length, 1);
              assert.equal(source.state, "closed");
            }
            await source.close();
          }
          const transport = new Transport(),
            states = [],
            failures = [];
          let calls = 0;
          const limited = pool(
            async () => {
              if (++calls === 1) return transport;
              const error = new RpcConnectError(`attempt ${calls}`);
              failures.push(error);
              throw error;
            },
            {
              reconnect: true,
              reconnectInitialDelayMs: 250,
              reconnectMaxDelayMs: 500,
              reconnectRandom: () => 0.5,
              reconnectMaxAttempts: 4,
              onStateChange: (...event) => states.push(event),
            },
          );
          await limited.get("a");
          transport.disconnect(new RpcConnectionLostError(false, 1012));
          for (const expected of [125, 250, 250]) {
            await until(() => timers.size === 1);
            const [id, timer] = [...timers.entries()][0];
            assert.equal(timer.delay, expected);
            timers.delete(id);
            timer.callback();
          }
          const last = await limited.closed;
          assert.equal(last, failures.at(-1));
          assert.equal(calls, 5);
          assert.equal(await limited.get("a").catch((e) => e), last);
          assert.deepEqual(
            states.map((e) => e[1]),
            ["connecting", "connected", "reconnecting", "closed"],
          );
          await limited.close();
          assert.equal(timers.size, 0);

          const first = new Transport();
          const permanent = new RpcConnectError("401", true, 401);
          let rejectedCalls = 0;
          const rejected = pool(
            async () => {
              if (++rejectedCalls === 1) return first;
              throw permanent;
            },
            { reconnect: true },
          );
          await rejected.get("a");
          first.disconnect(new RpcConnectionLostError(false, 1001));
          assert.equal(await rejected.closed, permanent);
          assert.equal(rejectedCalls, 2);
          await rejected.close();

          const opening = deferred();
          let sharedCalls = 0,
            openerSignal;
          const shared = pool(async (_, signal) => {
            sharedCalls++;
            openerSignal = signal;
            return opening.promise;
          });
          const core = new RpcClientCore(shared);
          const abort = new AbortController();
          const a = core
            .request(defineRoute({ method: "work" }), undefined, {
              signal: abort.signal,
            })
            .catch((e) => e);
          const b = core.request(defineRoute({ method: "work" }));
          abort.abort("one waiter");
          assert.equal(await a, "one waiter");
          assert.equal(openerSignal.aborted, false);
          opening.resolve(new Transport());
          assert.equal(await b, "ok");
          assert.equal(sharedCalls, 1);
          await core.close();

          const late = deferred(),
            closingStates = [];
          const closing = pool(() => late.promise, {
            onStateChange: (...event) => closingStates.push(event),
          });
          const pending = closing.get("a").catch((e) => e);
          await closing.close();
          assert.ok((await pending) instanceof RpcConnectionLostError);
          const orphan = new Transport();
          late.resolve(orphan);
          await until(() => orphan.closedByClient);
          assert.deepEqual(
            closingStates.map((e) => e[1]),
            ["connecting", "closed"],
          );

          const backoffTransport = new Transport();
          let backoffCalls = 0;
          const backoff = pool(
            async () => {
              if (++backoffCalls === 1) return backoffTransport;
              throw new RpcConnectError("temporary");
            },
            { reconnect: true, reconnectRandom: () => 0.5 },
          );
          await backoff.get("a");
          backoffTransport.disconnect(new RpcConnectionLostError(false, 1013));
          await until(() => timers.size === 1);
          await backoff.close();
          assert.equal(timers.size, 0);
          assert.equal(backoffCalls, 2);

          const live = { a: new Transport(), b: new Transport() };
          const multiple = new RpcTransportPool({
            endpoints: Object.keys(live).map((server) => ({ server, url: server })),
            open: async (endpoint) => live[endpoint.server],
            reconnect: true,
            requestTimeoutMs: null,
          });
          await multiple.openAll();
          const policy = new RpcConnectionLostError(false, 1008, "forbidden");
          live.a.disconnect(policy);
          assert.equal(await multiple.closed, policy);
          assert.equal(multiple.states.get("a"), "closed");
          assert.equal(multiple.states.get("b"), "connected");
          assert.equal(await multiple.get("b"), live.b);
          await multiple.close();

          const connectGate = deferred();
          let wireSignal;
          const timed = pool(() => connectGate.promise, { requestTimeoutMs: 500 });
          const timedCore = new RpcClientCore(timed);
          const timedCall = timedCore
            .request(defineRoute({ method: "slow" }))
            .catch((e) => e);
          const slow = new Transport();
          slow.request = (_, __, options) => {
            wireSignal = options.signal;
            return new Promise((_, reject) =>
              options.signal.addEventListener(
                "abort",
                () => reject(options.signal.reason),
                { once: true },
              ),
            );
          };
          connectGate.resolve(slow);
          await until(() => wireSignal !== undefined && timers.size === 1);
          const [id, budget] = [...timers.entries()][0];
          assert.equal(budget.delay, 500);
          timers.delete(id);
          budget.callback();
          assert.ok((await timedCall) instanceof RpcRequestTimeoutError);
          assert.equal(wireSignal.aborted, true);
          await timedCore.close();
          assert.equal(timers.size, 0);

          class Socket {
            readyState = 0;
            listeners = new Map();
            addEventListener(type, fn) {
              const list = this.listeners.get(type) ?? new Set();
              list.add(fn);
              this.listeners.set(type, list);
            }
            removeEventListener(type, fn) {
              this.listeners.get(type)?.delete(fn);
            }
            send() {}
            close() {
              this.readyState = 3;
            }
          }
          const socket = new Socket();
          const openingAbort = new AbortController();
          const socketOpen = WebSocketTransport.open("ws://test", {
            socketFactory: () => socket,
            signal: openingAbort.signal,
          }).catch((e) => e);
          openingAbort.abort("close opening");
          assert.equal(await socketOpen, "close opening");
          assert.equal(socket.readyState, 3);
          assert.equal(
            [...socket.listeners.values()].reduce(
              (n, listeners) => n + listeners.size,
              0,
            ),
            0,
          );
          assert.equal(timers.size, 0);

          const callbacks = pool(async () => new Transport(), {
            onStateChange() {
              throw new Error("callback failed");
            },
          });
          await callbacks.get("a");
          await callbacks.close();
          assert.equal(await callbacks.closed, undefined);
          const client = await GreetingClient.connect({ lazy: true });
          assert.equal(client.state, "connecting");
          assert.equal(client.states.get("rpc"), "connecting");
          await client.close();
          assert.equal(await client.closed, undefined);
          assert.equal(client.state, "closed");
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
