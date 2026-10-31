import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  type Feedback,
  type ConnectOptions,
  type MessageHandlers,
  type QueryParams,
  PracticeRtcClient,
  type PracticeSession,
  RtcError,
  closeReason,
  decodeMessage,
  encodeMessage,
  sessionUrl,
  sessionApi,
  type SessionDescriptor,
} from "../../examples/practice/client/index.js";
import { FakeDataChannel, FakePeerConnection, FakeTrack, FakeWebSocket, flush, hello, reset } from "./fakes.js";

function client(query?: Record<string, string>): PracticeRtcClient {
  return new PracticeRtcClient({
    baseUrl: "https://tutor.example.org/api",
    WebSocket: FakeWebSocket as unknown as typeof WebSocket,
    RTCPeerConnection: FakePeerConnection as unknown as typeof RTCPeerConnection,
    ...(query === undefined ? {} : { query }),
  });
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

/** Drive the server side of a practice session until the client is open. */
async function openPractice(
  connecting: Promise<PracticeSession>,
): Promise<{ session: PracticeSession; socket: FakeWebSocket; peer: FakePeerConnection }> {
  const socket = FakeWebSocket.last();
  socket.open();
  socket.receive(hello());
  await flush();
  const peer = FakePeerConnection.last();
  const offer = socket.messages("description")[0];
  assert.ok(offer !== undefined, "the answerer session expects a client offer");
  socket.receive({ type: "description", description: { type: "answer", sdp: "server-answer" }, negotiation: offer.negotiation });
  await flush();
  peer.openChannels();
  socket.receive({ type: "ready" });
  const session = await connecting;
  return { session, socket, peer };
}

function channel(peer: FakePeerConnection, name: string): FakeDataChannel {
  const found = peer.channels.find((c) => c.label === name);
  assert.ok(found !== undefined, `channel ${name}`);
  return found;
}

beforeEach(reset);
afterEach(reset);

describe("helpers", () => {
  test("session URLs substitute path parameters and append queries", () => {
    const url = sessionUrl(
      "http://localhost:8000/base/",
      {
        path: "/rtc/lessons/{lesson_id}/{rest:path}",
        parameters: [
          { name: "lesson_id", key: "lessonId", in: "path", required: true },
          { name: "rest", key: "rest", in: "path", required: true },
          { name: "tags", key: "tags", in: "query", required: false },
          { name: "page", key: "page", in: "query", required: false },
        ],
      },
      { lessonId: "a b/c", rest: "x/y z", tags: ["one", "two"] },
      { token: "t" },
      { token: "override" },
    );
    assert.equal(url, "ws://localhost:8000/base/rtc/lessons/a%20b%2Fc/x/y%20z?token=override&tags=one&tags=two");
  });

  test("required query parameters use final merged credentials", () => {
    const descriptor = {
      path: "/s", parameters: [{ name: "access_token", key: "accessToken", in: "query", required: true }],
    } as const;
    assert.equal(sessionUrl("https://x.test", descriptor, {}, { access_token: "global" }), "wss://x.test/s?access_token=global");
    assert.equal(sessionUrl("https://x.test", descriptor, {}, { access_token: "old" }, { access_token: "query" }), "wss://x.test/s?access_token=query");
    assert.equal(sessionUrl("https://x.test", descriptor, { accessToken: "named" }, { access_token: "old" }, { access_token: "query" }), "wss://x.test/s?access_token=named");
    assert.throws(() => sessionUrl("https://x.test", descriptor, {}), /Missing query parameter accessToken/);
    assert.throws(() => sessionUrl("https://x.test", descriptor, { accessToken: null }, { access_token: "global" }), /Missing query parameter accessToken/);
  });

  test("required credentials support shared tokens, refresh and explicit overrides", async () => {
    const descriptor: SessionDescriptor = {
      name: "secured", path: "/s", role: "answerer",
      media: { audio: null, video: null }, channels: [],
      parameters: [{ name: "token", key: "token", in: "query", required: true }],
    };
    let token = "first";
    let calls = 0;
    const config = { ...client().config, query: async () => { calls += 1; return { token }; } };
    type SecuredOptions = ConnectOptions<Record<never, never>> & { readonly token?: string };
    const api = sessionApi<Record<never, never>, SecuredOptions>(config, descriptor);
    for (const [options, expected] of [
      [{}, "first"],
      [{ query: { token: "local-query" } }, "local-query"],
      [{ token: "named", query: { token: "local-query" } }, "named"],
      [{}, "refreshed"],
    ] as const) {
      if (expected === "refreshed") token = expected;
      const connecting = api.connect(options);
      await flush();
      const socket = FakeWebSocket.last();
      assert.equal(new URL(socket.url).searchParams.get("token"), expected);
      socket.open();
      socket.serverClose(1001);
      await assert.rejects(connecting, { code: "shutdown" });
    }
    assert.equal(calls, 4);
  });

  test("session URLs reject missing parameters and unsupported origins", () => {
    const descriptor = {
      path: "/s/{id}",
      parameters: [{ name: "id", key: "id", in: "path", required: true }] as const,
    };
    assert.throws(() => sessionUrl("https://x.test", descriptor, {}), /Missing path parameter id/);
    assert.throws(() => sessionUrl("ftp://x.test", descriptor, { id: 1 }), /Unsupported baseUrl/);
    assert.equal(sessionUrl("wss://x.test", descriptor, { id: 1 }), "wss://x.test/s/1");
  });

  test("close codes map to error categories", () => {
    assert.equal(closeReason(4000, "session_limit", true).code, "session_limit");
    assert.equal(closeReason(1000, "", true).code, "closed");
    assert.equal(closeReason(1012, "", true).code, "shutdown");
    assert.equal(closeReason(1006, "", true).code, "signaling_lost");
    assert.equal(closeReason(1006, "", false).code, "connection_failed");
    assert.equal(closeReason(1008, "nope", true).code, "protocol_error");
  });

  test("messages are encoded and decoded per format", () => {
    assert.equal(encodeMessage("json", { a: 1 }), '{"a":1}');
    assert.equal(encodeMessage("text", "hi"), "hi");
    const bytes = new Uint8Array([1, 2]);
    assert.equal(encodeMessage("binary", bytes), bytes);
    assert.throws(() => encodeMessage("text", 1), RtcError);
    assert.throws(() => encodeMessage("binary", "x"), RtcError);
    assert.throws(() => encodeMessage("json", undefined), RtcError);
    assert.deepEqual(decodeMessage("json", '{"a":1}'), { a: 1 });
    assert.throws(() => decodeMessage("json", "{"), RtcError);
    assert.throws(() => decodeMessage("text", new ArrayBuffer(1)), RtcError);
    assert.equal(decodeMessage("raw", "x"), "x");
  });
});

describe("answerer sessions", () => {
  test("connect negotiates, sends typed messages and never stops borrowed tracks", async () => {
    const microphone = new FakeTrack("audio");
    const received: Feedback[] = [];
    const states: string[] = [];
    const connecting = client({ token: "demo" }).practice.connect({
      lessonId: "greetings",
      tracks: [microphone.asTrack()],
      onMessage: { messages: (message) => void received.push(message) },
      onStateChange: (state) => void states.push(state),
    });
    const socket = FakeWebSocket.last();
    assert.equal(socket.url, "wss://tutor.example.org/api/rtc/lessons/greetings?token=demo");
    assert.deepEqual(socket.protocols, ["fastapi-webrtc.v1"]);
    const { session, peer } = await openPractice(connecting);

    assert.equal(session.id, "s-1");
    assert.equal(session.state, "open");
    assert.deepEqual(peer.configuration?.iceServers, [{ urls: ["stun:stun.example.org"] }]);
    const messages = channel(peer, "messages");
    assert.deepEqual(messages.init, { ordered: true, negotiated: true, id: 0 });
    assert.equal(peer.transceivers[0]?.direction, "sendrecv");
    assert.equal(peer.transceivers[0]?.sender.track, microphone.asTrack());
    assert.equal(socket.messages("description")[0]?.negotiation, 1);

    await session.channels.messages.send({ text: "hello" });
    assert.deepEqual(messages.sent, ['{"text":"hello"}']);
    messages.receive('{"lesson":"Greetings","text":"ok","words":1,"audio_frames":3}');
    await flush();
    assert.equal(received[0]?.audio_frames, 3);

    peer.emitCandidate({ candidate: "candidate:1", sdpMid: "0" });
    peer.emitCandidate(null);
    assert.deepEqual(socket.messages("candidate"), [
      { type: "candidate", candidate: { candidate: "candidate:1", sdpMid: "0" } },
      { type: "candidate", candidate: null },
    ]);

    await session.close();
    await session.close();
    assert.equal(session.state, "closed");
    assert.deepEqual(socket.closedWith, { code: 1000, reason: undefined });
    assert.equal(peer.closed, true);
    assert.equal(microphone.stopped, false);
    assert.equal((await session.closed).code, "closed");
    assert.deepEqual(states, ["open", "closed"]);
    await assert.rejects(session.channels.messages.send({ text: "late" }), { code: "closed" });
  });

  test("remote candidates wait for the remote description", async () => {
    const connecting = client().practice.connect({ lessonId: "x" });
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(hello());
    socket.receive({ type: "candidate", candidate: { candidate: "candidate:9", sdpMid: "0" } });
    socket.receive({ type: "candidate", candidate: null });
    await flush();
    const peer = FakePeerConnection.last();
    assert.deepEqual(peer.candidates, []);
    socket.receive({ type: "description", description: { type: "answer", sdp: "a" }, negotiation: 1 });
    await flush();
    assert.deepEqual(peer.candidates, [{ candidate: "candidate:9", sdpMid: "0" }, null]);
    peer.openChannels();
    socket.receive({ type: "ready" });
    await connecting;
  });

  test("stale answers are ignored", async () => {
    const connecting = client().practice.connect({ lessonId: "x" });
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(hello());
    await flush();
    const peer = FakePeerConnection.last();
    socket.receive({ type: "description", description: { type: "answer", sdp: "old" }, negotiation: 7 });
    await flush();
    assert.equal(peer.signalingState, "have-local-offer");
    socket.receive({ type: "description", description: { type: "answer", sdp: "new" }, negotiation: 1 });
    await flush();
    assert.equal(peer.signalingState, "stable");
    assert.equal(peer.remoteDescription?.sdp, "new");
    peer.openChannels();
    socket.receive({ type: "ready" });
    await connecting;
  });

  test("the client rolls back its own offer when the server offers at the same time", async () => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    const added = session.addTrack(new FakeTrack("audio").asTrack());
    await flush();
    const ours = socket.messages("description").at(-1);
    assert.equal(ours?.negotiation, 2);
    assert.equal(peer.signalingState, "have-local-offer");

    socket.receive({ type: "description", description: { type: "offer", sdp: "server-offer" }, negotiation: 5 });
    await flush();
    const answer = socket.messages("description").find((m) => m.negotiation === 5);
    assert.deepEqual((answer?.description as { type: string }).type, "answer");
    assert.ok(peer.log.includes("local:rollback"));
    // The rolled-back offer is retried once the server's negotiation is done.
    const retry = socket.messages("description").at(-1);
    assert.equal(retry?.negotiation, 3);
    socket.receive({ type: "description", description: { type: "answer", sdp: "a3" }, negotiation: 3 });
    await added;
    assert.equal(peer.signalingState, "stable");
    await session.close();
  });

  test("non-terminal errors reach onError and invalid messages are reported", async () => {
    const errors: RtcError[] = [];
    const { session, socket, peer } = await openPractice(
      client().practice.connect({ lessonId: "x", onError: (error) => void errors.push(error) }),
    );
    socket.receive({ type: "error", code: "invalid_message", message: "bad", channel: "messages" });
    channel(peer, "messages").receive("not json");
    await flush();
    assert.deepEqual(
      errors.map((e) => [e.code, e.channel]),
      [
        ["invalid_message", "messages"],
        ["invalid_message", "messages"],
      ],
    );
    assert.equal(session.state, "open");
    await session.close();
  });

  test("handler failures are reported without closing the session", async () => {
    const errors: RtcError[] = [];
    const { session, peer } = await openPractice(
      client().practice.connect({
        lessonId: "x",
        onMessage: {
          messages: () => {
            throw new Error("boom");
          },
        },
        onError: (error) => void errors.push(error),
      }),
    );
    channel(peer, "messages").receive('{"lesson":"l","text":"t","words":1,"audio_frames":0}');
    await flush();
    assert.equal(errors[0]?.code, "handler_failure");
    assert.equal(session.state, "open");
    await session.close();
  });

  test("oversized messages are rejected before sending", async () => {
    const { session, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    await assert.rejects(session.channels.messages.send({ text: "x".repeat(100) }), { code: "message_too_large" });
    assert.deepEqual(channel(peer, "messages").sent, []);
    await session.close();
  });

  test("send waits while the buffer is above the high-water mark", async () => {
    const { session, peer } = await openPractice(
      client().practice.connect({ lessonId: "x", sendBufferHighBytes: 10 }),
    );
    const messages = channel(peer, "messages");
    messages.bufferedAmount = 50;
    let sent = false;
    const sending = session.channels.messages.send({ text: "a" }).then(() => {
      sent = true;
    });
    await flush();
    assert.equal(sent, false);
    messages.drain();
    await sending;
    assert.deepEqual(messages.sent, ['{"text":"a"}']);
    await session.close();
  });

  test("the server's termination reason rejects pending sends and resolves closed", async () => {
    const reasons: string[] = [];
    const { session, socket, peer } = await openPractice(
      client().practice.connect({ lessonId: "x", onClose: (reason) => void reasons.push(reason.code) }),
    );
    channel(peer, "messages").bufferedAmount = 10 ** 9;
    const pending = session.channels.messages.send({ text: "a" });
    socket.serverClose(1011, "engine_failure");
    await assert.rejects(pending, { code: "engine_failure" });
    const reason = await session.closed;
    assert.equal(reason.code, "engine_failure");
    assert.equal(reason.closeCode, 1011);
    assert.deepEqual(reasons, ["engine_failure"]);
  });
});

describe("operation deadlines", () => {
  test("unanswered renegotiation expires without stale answers extending its deadline", async (t) => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const microphone = new FakeTrack("audio");
    const adding = assert.rejects(session.addTrack(microphone.asTrack()), { code: "negotiation_timeout" });
    await flush();
    const removing = assert.rejects(session.removeTrack(microphone.asTrack()), { code: "negotiation_timeout" });
    t.mock.timers.tick(14_999);
    socket.receive({ type: "description", description: { type: "answer", sdp: "stale" }, negotiation: 1 });
    await flush();
    assert.equal(session.state, "open");
    t.mock.timers.tick(1);
    await Promise.all([adding, removing]);
    assert.equal((await session.closed).code, "negotiation_timeout");
    assert.equal(socket.closedWith?.reason, "negotiation_timeout");
    assert.equal(peer.closed, true);
    assert.equal(microphone.stopped, false);
  });

  test("a stalled local offer cannot keep a track operation pending", async (t) => {
    const { session, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    t.mock.timers.enable({ apis: ["setTimeout"] });
    peer.createOffer = () => new Promise(() => {});
    const rejected = assert.rejects(session.addTrack(new FakeTrack("audio").asTrack()), { code: "negotiation_timeout" });
    await flush();
    t.mock.timers.tick(15_000);
    await rejected;
  });

  test("server-initiated renegotiation has a deadline too", async (t) => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    t.mock.timers.enable({ apis: ["setTimeout"] });
    peer.createAnswer = () => new Promise(() => {});
    socket.receive({ type: "description", description: { type: "offer", sdp: "offer" }, negotiation: 2 });
    await flush();
    t.mock.timers.tick(15_000);
    assert.equal((await session.closed).code, "negotiation_timeout");
  });

  for (const failure of ["timeout", "transport"] as const) {
    test(`a stalled replaceTrack is interrupted by ${failure}`, async (t) => {
      const { session, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
      t.mock.timers.enable({ apis: ["setTimeout"] });
      peer.getTransceivers()[0]!.sender.replaceTrack = () => new Promise(() => {});
      const rejected = assert.rejects(session.addTrack(new FakeTrack("audio").asTrack()), {
        code: failure === "timeout" ? "negotiation_timeout" : "transport_lost",
      });
      await flush();
      if (failure === "timeout") t.mock.timers.tick(15_000);
      else peer.changeConnectionState("failed");
      await rejected;
    });
  }

  test("a full buffer times out all pending sends even after repeated wakeups", async (t) => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const messages = channel(peer, "messages");
    messages.bufferedAmount = 10 ** 9;
    const sending = [1, 2].map(() => assert.rejects(session.channels.messages.send({ text: "a" }), { code: "slow_consumer", channel: "messages" }));
    t.mock.timers.tick(9_999);
    messages.onbufferedamountlow?.();
    await flush();
    assert.equal(session.state, "open");
    t.mock.timers.tick(1);
    await Promise.all(sending);
    assert.equal((await session.closed).code, "slow_consumer");
    assert.equal(socket.closedWith?.reason, "slow_consumer");
    assert.deepEqual(messages.sent, []);
  });

  test("successful sends and negotiations cancel their timers; later sends get a fresh deadline", async (t) => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x", sendTimeoutMs: 100 }));
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const adding = session.addTrack(new FakeTrack("audio").asTrack());
    await flush();
    socket.receive({ type: "description", description: { type: "answer", sdp: "a2" }, negotiation: 2 });
    await adding;
    const messages = channel(peer, "messages");
    messages.bufferedAmount = 10 ** 9;
    const sending = session.channels.messages.send({ text: "a" });
    t.mock.timers.tick(99);
    messages.drain();
    await sending;
    t.mock.timers.tick(30_000);
    assert.equal(session.state, "open");
    messages.bufferedAmount = 10 ** 9;
    const rejected = assert.rejects(session.channels.messages.send({ text: "b" }), { code: "slow_consumer" });
    t.mock.timers.tick(100);
    await rejected;
  });

  for (const state of ["failed", "closed"] as const) {
    test(`transport ${state} rejects pending sends and renegotiation with signaling still open`, async () => {
      const { session, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
      channel(peer, "messages").bufferedAmount = 10 ** 9;
      const sending = assert.rejects(session.channels.messages.send({ text: "a" }), { code: "transport_lost" });
      const adding = assert.rejects(session.addTrack(new FakeTrack("audio").asTrack()), { code: "transport_lost" });
      await flush();
      peer.changeConnectionState("disconnected");
      assert.equal(session.state, "open");
      peer.changeConnectionState(state);
      await Promise.all([sending, adding]);
      assert.equal((await session.closed).code, "transport_lost");
    });
  }
});

describe("connection failures", () => {
  test("an admission denial rejects connect", async () => {
    const closes: string[] = [];
    const connecting = client().practice.connect({ lessonId: "x", onClose: (r) => void closes.push(r.code) });
    FakeWebSocket.last().serverClose(1006);
    await assert.rejects(connecting, { code: "connection_failed" });
    assert.deepEqual(closes, ["connection_failed"]);
    assert.equal(FakePeerConnection.instances.length, 0);
  });

  test("unsupported tracks are rejected before connecting", async () => {
    await assert.rejects(
      client().practice.connect({ lessonId: "x", tracks: [new FakeTrack("video").asTrack()] }),
      { code: "media_rejected" },
    );
    assert.equal(FakeWebSocket.instances.length, 0);
  });

  test("an aborted signal stops the connection", async () => {
    const controller = new AbortController();
    const connecting = client().practice.connect({ lessonId: "x", signal: controller.signal });
    FakeWebSocket.last().open();
    controller.abort();
    await assert.rejects(connecting, { code: "aborted" });
    assert.equal(FakeWebSocket.last().closedWith?.reason, "aborted");

    const aborted = AbortSignal.abort();
    await assert.rejects(client().practice.connect({ lessonId: "x", signal: aborted }), { code: "aborted" });
  });

  test("the connection deadline rejects with negotiation_timeout", async () => {
    const connecting = client().practice.connect({ lessonId: "x", timeoutMs: 5 });
    FakeWebSocket.last().open();
    await assert.rejects(connecting, { code: "negotiation_timeout" });
  });

  for (const reason of ["abort", "timeout"] as const) {
    for (const settle of ["resolve", "reject"] as const) {
      test(`${reason} cancels pending credentials before their late ${settle}`, async () => {
        const credentials = deferred<QueryParams>();
        const controller = new AbortController();
        const configured = new PracticeRtcClient({
          baseUrl: "https://example.test",
          query: () => credentials.promise,
          WebSocket: FakeWebSocket as unknown as typeof WebSocket,
          RTCPeerConnection: FakePeerConnection as unknown as typeof RTCPeerConnection,
        });
        const connecting = configured.practice.connect({
          lessonId: "x", signal: controller.signal,
          timeoutMs: reason === "timeout" ? 5 : 1000,
        });
        const rejected = assert.rejects(connecting, {
          code: reason === "abort" ? "aborted" : "negotiation_timeout",
        });
        if (reason === "abort") controller.abort("cancelled");
        await rejected;
        assert.equal(FakeWebSocket.instances.length, 0);
        if (settle === "resolve") credentials.resolve({ token: "late" });
        else credentials.reject(new Error("late credentials failure"));
        await flush();
        assert.equal(FakeWebSocket.instances.length, 0);
        assert.equal(FakePeerConnection.instances.length, 0);
      });
    }
  }

  test("credential resolution consumes the connection deadline", async (context) => {
    context.mock.timers.enable({ apis: ["setTimeout", "Date"] });
    const credentials = deferred<QueryParams>();
    const configured = new PracticeRtcClient({
      ...client().config, query: () => credentials.promise,
    });
    const connecting = configured.practice.connect({ lessonId: "x", timeoutMs: 100 });
    const rejected = assert.rejects(connecting, { code: "negotiation_timeout" });
    context.mock.timers.tick(60);
    credentials.resolve({ token: "t" });
    await Promise.resolve();
    assert.equal(FakeWebSocket.instances.length, 1);
    context.mock.timers.tick(40);
    await rejected;
    assert.equal(FakeWebSocket.last().closedWith?.reason, "negotiation_timeout");
  });

  test("a mismatched server contract is a protocol error", async () => {
    const connecting = client().practice.connect({ lessonId: "x" });
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(hello({ channels: [] }));
    await assert.rejects(connecting, { code: "protocol_error", message: /regenerate/ });
    assert.equal(socket.closedWith?.code, 4000);
  });

  test("unexpected signaling messages fail the session", async () => {
    const first = client().practice.connect({ lessonId: "x" });
    FakeWebSocket.last().open();
    FakeWebSocket.last().receive({ type: "ready" });
    await assert.rejects(first, { code: "protocol_error" });

    const second = client().practice.connect({ lessonId: "x" });
    FakeWebSocket.last().open();
    FakeWebSocket.last().receive("{not json");
    await assert.rejects(second, { code: "protocol_error" });

    const third = client().practice.connect({ lessonId: "x" });
    FakeWebSocket.last().open();
    FakeWebSocket.last().receive(hello({ protocol: 2 }));
    await assert.rejects(third, { code: "unsupported_protocol" });
  });
});

describe("offerer sessions", () => {
  test("the client answers server offers and attaches receive-only media", async () => {
    const ticks: number[] = [];
    const tracks: string[] = [];
    const connecting = client().clock.connect({
      intervalMs: 100,
      onMessage: { ticks: (tick) => void ticks.push(tick.sequence) },
      onTrack: ({ track }) => void tracks.push(track.kind),
    });
    const socket = FakeWebSocket.last();
    assert.equal(socket.url, "wss://tutor.example.org/api/rtc/clock?interval_ms=100");
    socket.open();
    socket.receive(
      hello({
        name: "clock",
        role: "offerer",
        media: { audio: null, video: "send", max_incoming_tracks: 4 },
        channels: [
          {
            name: "ticks",
            id: 0,
            negotiated: true,
            ordered: true,
            max_retransmits: null,
            max_packet_life_time_ms: null,
            max_message_bytes: 65536,
            receive_format: "none",
            send_format: "json",
          },
        ],
      }),
    );
    await flush();
    assert.deepEqual(socket.messages("description"), []);
    const peer = FakePeerConnection.last();
    peer.remoteTransceiver("video", "recvonly");
    socket.receive({ type: "description", description: { type: "offer", sdp: "server-offer" }, negotiation: 1 });
    await flush();
    const answer = socket.messages("description")[0];
    assert.equal(answer?.negotiation, 1);
    assert.equal((answer?.description as { type: string }).type, "answer");
    peer.emitTrack("video");
    peer.openChannels();
    socket.receive({ type: "ready" });
    const session = await connecting;
    channel(peer, "ticks").receive('{"sequence":4,"lesson_count":2}');
    await flush();
    assert.deepEqual(ticks, [4]);
    assert.deepEqual(tracks, ["video"]);
    // @ts-expect-error The server does not receive ticks, so the client cannot send them.
    void session.channels.ticks.send;
    assert.equal(session.remoteTracks.length, 1);
    await session.close();
  });

  test("in-band channels from the server are bound by label", async () => {
    const connecting = client().clock.connect({});
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(
      hello({
        name: "clock",
        role: "offerer",
        media: { audio: null, video: "send", max_incoming_tracks: 4 },
        channels: [
          {
            name: "ticks",
            id: null,
            negotiated: false,
            ordered: true,
            max_retransmits: null,
            max_packet_life_time_ms: null,
            max_message_bytes: 65536,
            receive_format: "none",
            send_format: "json",
          },
        ],
      }),
    );
    await flush();
    const peer = FakePeerConnection.last();
    assert.equal(peer.channels.length, 0);
    const stranger = peer.remoteChannel("other");
    assert.equal(stranger.readyState, "closed");
    const ticks = peer.remoteChannel("ticks");
    socket.receive({ type: "description", description: { type: "offer", sdp: "o" }, negotiation: 1 });
    await flush();
    ticks.open();
    socket.receive({ type: "ready" });
    const session = await connecting;
    assert.equal(session.channels.ticks.isOpen, true);
    assert.equal(session.channels.ticks.dataChannel, ticks as unknown as RTCDataChannel);
    await session.close();
  });
});

describe("session API", () => {
  test("tracks, subscriptions and stats", async () => {
    const { session, socket, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    const seen: string[] = [];
    const unsubscribe = session.channels.messages.subscribe((message) => void seen.push(message.text));
    const messages = channel(peer, "messages");
    messages.receive('{"lesson":"l","text":"one","words":1,"audio_frames":0}');
    unsubscribe();
    messages.receive('{"lesson":"l","text":"two","words":1,"audio_frames":0}');
    await flush();
    assert.deepEqual(seen, ["one"]);
    assert.equal(session.channels.messages.bufferedAmount, 0);

    const microphone = new FakeTrack("audio").asTrack();
    const adding = session.addTrack(microphone);
    await flush();
    socket.receive({ type: "description", description: { type: "answer", sdp: "a2" }, negotiation: 2 });
    await adding;
    assert.ok(peer.getSenders().some((sender) => sender.track === microphone));

    const removing = session.removeTrack(microphone);
    await flush();
    socket.receive({ type: "description", description: { type: "answer", sdp: "a3" }, negotiation: 3 });
    await removing;
    assert.ok(!peer.getSenders().some((sender) => sender.track === microphone));
    await session.removeTrack(microphone);

    await assert.rejects(session.addTrack(new FakeTrack("video").asTrack()), { code: "media_rejected" });
    assert.ok((await session.stats()) instanceof Map);
    assert.equal(session.peerConnection, peer as unknown as RTCPeerConnection);
    await session.close();
    await assert.rejects(session.addTrack(microphone), { code: "closed" });
  });

  test("config queries, RTC configuration and credentials", async () => {
    const practice = new PracticeRtcClient({
      baseUrl: "http://localhost",
      query: async () => ({ token: "dynamic" }),
      rtcConfiguration: { bundlePolicy: "max-bundle" },
      WebSocket: FakeWebSocket as unknown as typeof WebSocket,
      RTCPeerConnection: FakePeerConnection as unknown as typeof RTCPeerConnection,
    }).practice;
    assert.equal(practice.descriptor.name, "practice");
    const connecting = practice.connect({ lessonId: "x" });
    await flush();
    const socket = FakeWebSocket.last();
    assert.equal(socket.url, "ws://localhost/rtc/lessons/x?token=dynamic");
    socket.open();
    socket.receive(hello({ ice_servers: [{ urls: ["turn:t.example.org"], username: "u", credential: "c" }] }));
    await flush();
    const peer = FakePeerConnection.last();
    assert.deepEqual(peer.configuration, {
      bundlePolicy: "max-bundle",
      iceServers: [{ urls: ["turn:t.example.org"], username: "u", credential: "c" }],
    });
    socket.serverClose(1001);
    await assert.rejects(connecting, { code: "shutdown" });
  });

  test("explicit ICE servers replace the server's list", async () => {
    const connecting = client().practice.connect({ lessonId: "x", rtcConfiguration: { iceServers: [] } });
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(hello());
    await flush();
    assert.deepEqual(FakePeerConnection.last().configuration?.iceServers, []);
    socket.serverClose(1000);
    await assert.rejects(connecting, { code: "closed" });
  });

  test("a wrong subprotocol or unknown messages fail the session", async () => {
    const first = client().practice.connect({ lessonId: "x" });
    FakeWebSocket.last().open("other");
    await assert.rejects(first, { code: "unsupported_protocol" });

    const second = client().practice.connect({ lessonId: "x" });
    const socket = FakeWebSocket.last();
    socket.open();
    socket.receive(hello());
    socket.receive({ type: "surprise" });
    await assert.rejects(second, { code: "protocol_error" });

    const third = client().practice.connect({ lessonId: "x" });
    FakeWebSocket.last().open();
    FakeWebSocket.last().onmessage?.({ data: new ArrayBuffer(1) } as MessageEvent);
    await assert.rejects(third, { code: "protocol_error" });
  });

  test("unknown server error codes are reported as protocol errors", async () => {
    const errors: string[] = [];
    const { session, socket } = await openPractice(
      client().practice.connect({ lessonId: "x", onError: (e) => void errors.push(e.code) }),
    );
    socket.receive({ type: "error", code: "mystery", message: "?" });
    assert.deepEqual(errors, ["protocol_error"]);
    await session.close();
  });

  test("aborting an open session closes it", async () => {
    const controller = new AbortController();
    const { session } = await openPractice(client().practice.connect({ lessonId: "x", signal: controller.signal }));
    controller.abort();
    assert.equal((await session.closed).code, "closed");
  });

  test("a closed channel rejects sends", async () => {
    const { session, peer } = await openPractice(client().practice.connect({ lessonId: "x" }));
    channel(peer, "messages").close();
    await assert.rejects(session.channels.messages.send({ text: "a" }), { code: "channel_closed" });
    await session.close();
  });

  test("offerer sessions attach local tracks to offered transceivers", async () => {
    const camera = new FakeTrack("audio");
    const connecting = client().practice.connect({ lessonId: "x", tracks: [camera.asTrack()] });
    const socket = FakeWebSocket.last();
    socket.open();
    // A server-offered session that also receives audio.
    socket.receive(hello({ role: "offerer" }));
    await flush();
    const peer = FakePeerConnection.last();
    assert.deepEqual(socket.messages("description"), []);
    peer.remoteTransceiver("audio", "recvonly");
    socket.receive({ type: "description", description: { type: "offer", sdp: "o" }, negotiation: 1 });
    await flush();
    assert.equal(peer.transceivers[0]?.sender.track, camera.asTrack());
    assert.equal(peer.transceivers[0]?.direction, "sendrecv");
    peer.openChannels();
    socket.receive({ type: "ready" });
    const session = await connecting;
    await session.close();
    assert.equal(camera.stopped, false);
  });
});

describe("message formats", () => {
  test("raw and binary payloads", () => {
    const buffer = new ArrayBuffer(2);
    assert.equal(encodeMessage("raw", "x"), "x");
    assert.equal(encodeMessage("raw", buffer), buffer);
    assert.throws(() => encodeMessage("raw", 1), RtcError);
    assert.equal(decodeMessage("binary", buffer), buffer);
    assert.throws(() => decodeMessage("binary", "x"), RtcError);
    assert.throws(() => decodeMessage("json", buffer), RtcError);
    assert.equal(decodeMessage("text", "t"), "t");
  });

  test("one-way channels reject both directions", () => {
    assert.throws(() => encodeMessage("none", "x"), { code: "invalid_message" });
    assert.throws(() => decodeMessage("none", "x"), { code: "invalid_message" });
    type Channels = {
      readonly upload: { readonly send: string; readonly receive: never };
      readonly news: { readonly send: never; readonly receive: string };
    };
    const handlers: MessageHandlers<Channels> = {
      news: () => undefined,
      // @ts-expect-error Send-only channels have no message handler.
      upload: () => undefined,
    };
    assert.ok(handlers);
  });
});
