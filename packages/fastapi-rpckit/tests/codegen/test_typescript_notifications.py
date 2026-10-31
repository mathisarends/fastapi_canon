import subprocess
from textwrap import dedent

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_notification_recovery_overflow_and_iterator_cleanup_in_node(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "notifications.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          RpcClientCore,
          RpcTransportPool,
          RpcConnectionLostError,
          defineNotification,
        } = require("./compiled/generated/core");
        const { WebSocketTransport } = require("./compiled/generated");
        async function until(condition) {
          for (let i = 0; i < 150; i++) {
            if (condition()) return;
            await Promise.resolve();
          }
          throw new Error("Notifications did not progress");
        }
        class Transport {
          values = [];
          waiters = [];
          active = 0;
          finished = false;
          closed = new Promise((resolve) => {
            this.resolveClosed = resolve;
          });
          notifications(signal) {
            this.active++;
            let returned = false;
            const transport = this;
            return {
              [Symbol.asyncIterator]() {
                return {
                  next: () => transport.next(signal),
                  async return() {
                    if (!returned) {
                      transport.active--;
                      returned = true;
                    }
                    return { done: true };
                  },
                };
              },
            };
          }
          next(signal) {
            if (signal?.aborted) return Promise.resolve({ done: true });
            if (this.values.length)
              return Promise.resolve({ done: false, value: this.values.shift() });
            if (this.error) return Promise.reject(this.error);
            return new Promise((resolve, reject) => {
              const abort = () => {
                this.waiters = this.waiters.filter((item) => item !== waiter);
                waiter.clean();
                resolve({ done: true });
              };
              const waiter = {
                resolve,
                reject,
                clean: () => signal?.removeEventListener("abort", abort),
              };
              this.waiters.push(waiter);
              signal?.addEventListener("abort", abort, { once: true });
            });
          }
          emit(value) {
            const message = { method: "update", params: { value } };
            const waiter = this.waiters.shift();
            if (waiter) {
              waiter.clean();
              waiter.resolve({ done: false, value: message });
            } else this.values.push(message);
          }
          drop() {
            this.error = new RpcConnectionLostError(false, 1012);
            this.resolveClosed(this.error);
            for (const waiter of this.waiters.splice(0)) {
              waiter.clean();
              waiter.reject(this.error);
            }
          }
          async request() {
            return "ok";
          }
          async close() {
            this.finished = true;
            this.resolveClosed();
          }
        }
        const route = defineNotification({ method: "update", server: "a" });
        function pool(factory, options = {}) {
          return new RpcTransportPool({
            endpoints: [{ server: "a", url: "ws://test" }],
            open: factory,
            requestTimeoutMs: null,
            ...options,
          });
        }
        async function main() {
          const transports = [],
            states = [];
          const source = pool(
            async () => {
              const transport = new Transport();
              transports.push(transport);
              return transport;
            },
            { reconnect: true, onStateChange: (...event) => states.push(event) },
          );
          const core = new RpcClientCore(source);
          const one = core.notifications(route)[Symbol.asyncIterator]();
          const two = core.notifications(route)[Symbol.asyncIterator]();
          for (let generation = 0; generation < 11; generation++) {
            const a = one.next(),
              b = two.next();
            if (generation) transports.at(-1).drop();
            await until(
              () =>
                transports.length === generation + 1 &&
                transports.at(-1).waiters.length === 1,
            );
            transports.at(-1).emit(generation);
            assert.deepEqual((await a).value, { value: generation });
            assert.deepEqual((await b).value, { value: generation });
            assert.equal(transports.at(-1).active, 1);
            assert.ok(
              transports.slice(0, -1).every((transport) => transport.active === 0),
            );
            assert.ok(
              transports
                .slice(0, -1)
                .every((transport) => transport.waiters.length === 0),
            );
          }
          assert.equal(
            states.filter((event) => event[1] === "reconnecting").length,
            10,
          );
          await one.return();
          await two.return();
          await core.close();
          assert.ok(transports.every((transport) => transport.active === 0));

          const overflowTransport = new Transport(),
            reported = [];
          const overflowCore = new RpcClientCore(overflowTransport, {
            notificationQueueSize: 1,
            onOverflow: (...event) => reported.push(event),
          });
          const stream = overflowCore.notifications(route)[Symbol.asyncIterator]();
          const first = stream.next();
          await until(() => overflowTransport.waiters.length === 1);
          overflowTransport.emit(1);
          assert.deepEqual((await first).value, { value: 1 });
          for (const value of [2, 3, 4]) overflowTransport.emit(value);
          await until(() => overflowCore.droppedNotifications === 2);
          assert.deepEqual(reported, [
            ["update", 1, "subscriber"],
            ["update", 2, "subscriber"],
          ]);
          assert.deepEqual((await stream.next()).value, { value: 4 });
          await stream.return();
          await overflowCore.close();

          for (const borrowed of [false, true]) {
            const transports = [];
            let openAgain;
            const source = pool(
              async () => {
                if (transports.length)
                  return new Promise((resolve) => {
                    openAgain = resolve;
                  });
                const transport = new Transport();
                transports.push(transport);
                return transport;
              },
              { reconnect: true },
            );
            const core = new RpcClientCore(source, { closeTransport: !borrowed });
            const left = core.notifications(route)[Symbol.asyncIterator]();
            const right = core.notifications(route)[Symbol.asyncIterator]();
            const a = left.next(),
              b = right.next();
            await until(
              () => transports.length === 1 && transports[0].waiters.length === 1,
            );
            transports[0].drop();
            await until(() => openAgain !== undefined);
            await left.return();
            assert.equal((await a).done, true);
            if (borrowed) {
              const next = new Transport();
              openAgain(next);
              await until(() => next.waiters.length === 1);
              next.emit("survivor");
              assert.deepEqual((await b).value, { value: "survivor" });
              const waiting = right.next();
              await core.close();
              assert.equal((await waiting).done, true);
              assert.equal(source.state, "connected");
            } else {
              await core.close();
              assert.equal((await b).done, true);
            }
            await right.return();
            await source.close();
          }

          class Socket {
            readyState = 1;
            listeners = new Map();
            addEventListener(type, fn) {
              const list = this.listeners.get(type) ?? [];
              list.push(fn);
              this.listeners.set(type, list);
            }
            send() {}
            close() {
              this.readyState = 3;
            }
            emit(value) {
              for (const fn of this.listeners.get("message") ?? [])
                fn({
                  data: JSON.stringify({
                    jsonrpc: "2.0",
                    method: "update",
                    params: { value },
                  }),
                });
            }
          }
          const socket = new Socket(),
            drops = [];
          const transport = await WebSocketTransport.open("ws://test", {
            socketFactory: () => socket,
            notificationQueueSize: 1,
            onOverflow: (...event) => drops.push(event),
          });
          transport.startNotifications();
          for (const value of [1, 2, 3]) socket.emit(value);
          assert.equal(transport.droppedNotifications, 2);
          assert.deepEqual(drops, [
            [undefined, 1, "transport"],
            [undefined, 2, "transport"],
          ]);
          const notifications = transport.notifications()[Symbol.asyncIterator]();
          assert.equal((await notifications.next()).value.params.value, 3);
          const signal = new AbortController();
          const waiting = transport
            .notifications(signal.signal)
            [Symbol.asyncIterator]()
            .next();
          signal.abort();
          assert.equal((await waiting).done, true);
          await transport.close();
          console.log("notifications complete");
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
    assert "notifications complete" in result.stdout, result.stdout + result.stderr
