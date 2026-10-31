import subprocess
from textwrap import dedent

from fastapi_canon.integrations.websockets.codegen.ir import build_ir

from .test_typescript_cancellation import cancellation_sdk as cancellation_sdk


def test_heartbeat_capability_requires_exact_wire_contract(document):
    assert build_ir(document).heartbeat
    assert not build_ir({**document, "x-rpc-heartbeat": {"method": "ping"}}).heartbeat
    assert not build_ir(
        {k: v for k, v in document.items() if k != "x-rpc-heartbeat"}
    ).heartbeat


def test_heartbeat_breaker_recovery_and_suspended_timers(cancellation_sdk):
    root, node = cancellation_sdk
    runner = root / "heartbeat.js"
    runner.write_text(
        dedent("""
        const assert = require("node:assert/strict");
        const {
          WebSocketTransport, RpcConnectionLostError, RpcRequestTimeoutError,
        } = require("./compiled/generated");
        const {RpcTransportPool} = require("./compiled/generated/core");
        let now = 0, serial = 0;
        Date.now = () => now;
        const timers = new Map();
        globalThis.setTimeout = (callback, delay) => {
          const id = ++serial; timers.set(id, {callback, at: now + delay}); return id;
        };
        globalThis.clearTimeout = id => timers.delete(id);
        function advance(ms) {
          now += ms;
          const due = [...timers].filter(([, t]) => t.at <= now);
          for (const [id, t] of due) if (timers.delete(id)) t.callback();
        }
        async function settle() {
          for (let i = 0; i < 40; i++) await Promise.resolve();
        }
        class Socket {
          readyState = 1; listeners = new Map(); sent = [];
          addEventListener(type, callback) { this.listeners.set(type, callback); }
          send(frame) { this.sent.push(JSON.parse(frame)); }
          close() { this.readyState = 3; }
          receive(message) {
            this.listeners.get("message")({data: JSON.stringify(message)});
          }
        }
        async function open(extra = {}) {
          const socket = new Socket();
          const transport = await WebSocketTransport.open("ws://test", {
            socketFactory: () => socket, requestTimeoutMs: null,
            heartbeatIntervalMs: 100, heartbeatTimeoutMs: 20, ...extra,
          });
          return {socket, transport};
        }
        async function main() {
          const sockets = [], states = [];
          const pool = new RpcTransportPool({
            endpoints: [{server: "rpc", url: "ws://test"}],
            requestTimeoutMs: null, reconnect: true,
            onStateChange: (_, state) => states.push(state),
            open: async () => {
              const pair = await open(); sockets.push(pair); return pair.transport;
            },
          });
          await pool.get("rpc"); advance(100);
          assert.equal(sockets[0].socket.sent[0].method, "$/ping");
          advance(20); await settle();
          assert.equal(sockets.length, 2);
          assert.equal(pool.state, "connected");
          assert(states.includes("reconnecting"));
          assert(await sockets[0].transport.closed instanceof RpcConnectionLostError);
          await pool.close(); assert.equal(timers.size, 0);

          const active = await open();
          advance(90);
          active.socket.receive({jsonrpc: "2.0", method: "news", params: {}});
          advance(90); assert.equal(active.socket.sent.length, 0);
          advance(10); const ping = active.socket.sent[0];
          active.socket.receive({jsonrpc: "2.0", id: ping.id, result: null});
          advance(100); assert.equal(active.socket.sent.length, 2);
          // A timer delayed by sleep sends only one fresh probe.
          advance(1000); assert.equal(active.socket.sent.length, 3);
          active.socket.receive({
            jsonrpc: "2.0", id: active.socket.sent[2].id, result: null,
          });
          await active.transport.close(); assert.equal(timers.size, 0);

          const older = await open(); advance(100);
          older.socket.receive({jsonrpc: "2.0", id: older.socket.sent[0].id,
            error: {code: -32601, message: "Method not found"}});
          advance(1000); assert.equal(older.socket.sent.length, 1);
          assert.equal(older.socket.readyState, 1); assert.equal(timers.size, 0);
          await older.transport.close();

          const breaker = await open({
            heartbeatIntervalMs: null, requestTimeoutMs: 10,
            consecutiveTimeoutLimit: 2,
          });
          const first = breaker.transport.request("work").catch(e => e);
          advance(10); assert(await first instanceof RpcRequestTimeoutError);
          breaker.socket.receive({jsonrpc: "2.0", id: 999, result: null});
          const second = breaker.transport.request("work").catch(e => e);
          advance(10); await second; assert.equal(breaker.socket.readyState, 1);
          const third = breaker.transport.request("work").catch(e => e);
          advance(10); await third;
          assert(await breaker.transport.closed instanceof RpcConnectionLostError);
          assert.equal(timers.size, 0);
          const disabled = await open({heartbeatIntervalMs: null});
          advance(1000); assert.equal(disabled.socket.sent.length, 0);
          await disabled.transport.close(); assert.equal(timers.size, 0);
          console.log("heartbeat complete");
        }
        main().catch(e => { console.error(e); process.exitCode = 1; });
    """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "heartbeat complete" in result.stdout
