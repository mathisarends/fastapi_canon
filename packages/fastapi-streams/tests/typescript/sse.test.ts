import assert from "node:assert/strict";
import { test } from "node:test";
import { createClient, HttpError, StreamParseError, TransportError } from "../../examples/agent_runs/generated/index.js";
import { parseSSE, stream, appendQuery, setHeader } from "../../examples/agent_runs/generated/transport/sse.js";

const encoder = new TextEncoder();

function bytes(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

function response(text: string): Response {
  return new Response(text, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

async function collect<T>(events: AsyncIterable<T>): Promise<T[]> {
  const result: T[] = [];
  for await (const event of events) result.push(event);
  return result;
}

test("SSE survives every byte boundary, fragmented UTF-8, multiline data and EOF", async () => {
  const raw = encoder.encode('\ufeff: heartbeat\r\nevent: token\r\nid: 1\r\nretry: 1000\r\ndata: {\r\ndata: "text":"世界 👋"}\r\n\r\n: ping\n\ndata: {"text":"end"}');
  const expected = [{ text: "世界 👋" }, { text: "end" }];
  for (let cut = 0; cut <= raw.length; cut++) {
    assert.deepEqual(await collect(parseSSE(bytes([raw.slice(0, cut), raw.slice(cut)]))), expected);
  }
  assert.deepEqual(await collect(parseSSE(bytes(Array.from(raw, byte => Uint8Array.of(byte))))), expected);
});

function referenceFrames(text: string): string[] {
  // The original character-by-character parser, kept as a test oracle.
  let line = "";
  let data: string[] = [];
  let skipLF = false;
  const frames: string[] = [];
  const accept = () => {
    const current = line;
    line = "";
    if (current === "") {
      if (data.length > 0) frames.push(data.join("\n"));
      data = [];
      return;
    }
    const colon = current.indexOf(":");
    const field = colon < 0 ? current : current.slice(0, colon);
    let value = colon < 0 ? "" : current.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
  };
  for (const char of text.replace(/^﻿/, "")) {
    if (skipLF) {
      skipLF = false;
      if (char === "\n") continue;
    }
    if (char === "\r" || char === "\n") {
      skipLF = char === "\r";
      accept();
    } else {
      line += char;
    }
  }
  if (line !== "") accept();
  if (data.length > 0) frames.push(data.join("\n"));
  return frames;
}

async function outcome(events: AsyncIterable<unknown>): Promise<unknown[]> {
  const result: unknown[] = [];
  try {
    for await (const event of events) result.push(event);
  } catch (error) {
    assert.ok(error instanceof StreamParseError);
    result.push({ invalid: error.data });
  }
  return result;
}

test("line splitting matches the character parser for random chunkings", async () => {
  const alphabet = ["data", ":", " ", "1", "世", "\r", "\n", "\r\n", "id", "﻿"];
  let seed = 20261025;
  const random = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return (seed >>> 0) / 2 ** 32;
  };
  for (let run = 0; run < 2000; run++) {
    let text = "";
    for (let index = Math.floor(random() * 40); index > 0; index--) {
      text += alphabet[Math.floor(random() * alphabet.length)];
    }
    const raw = encoder.encode(text);
    const cuts = Array.from({ length: Math.floor(random() * 6) }, () => Math.floor(random() * (raw.length + 1)));
    const edges = [0, ...cuts.sort((a, b) => a - b), raw.length];
    const chunks = edges.slice(1).map((end, index) => raw.slice(edges[index], end));
    const expected: unknown[] = [];
    for (const frame of referenceFrames(text)) {
      try {
        expected.push(JSON.parse(frame));
      } catch {
        expected.push({ invalid: frame });
        break;
      }
    }
    assert.deepEqual(await outcome(parseSSE(bytes(chunks))), expected, JSON.stringify(text));
  }
});

test("a line spanning many chunks is joined once", async () => {
  const raw = encoder.encode(`data: "${"x".repeat(100_000)}"\r`);
  const chunks: Uint8Array[] = [];
  for (let index = 0; index < raw.length; index += 7) chunks.push(raw.slice(index, index + 7));
  chunks.push(encoder.encode("\n\n"));
  assert.deepEqual(await collect(parseSSE(bytes(chunks))), ["x".repeat(100_000)]);
});

test("LF, CRLF, CR, final newlines and metadata-only frames", async () => {
  for (const newline of ["\n", "\r\n", "\r"]) {
    const text = [": ping", "", "event: ignored", "id: abc", "retry: 2", "", "data: 1", "", "data: 2", ""].join(newline);
    assert.deepEqual(await collect(parseSSE(bytes([encoder.encode(text)]))), [1, 2]);
  }
  assert.deepEqual(await collect(parseSSE(bytes([encoder.encode(": ping\n\n")]))), []);
});

test("events are yielded while the response remains open", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const events = parseSSE<{ n: number }>(body);
  const first = events.next();
  controller.enqueue(encoder.encode('data: {"n":1}\n\n'));
  assert.deepEqual((await first).value, { n: 1 });
  controller.enqueue(encoder.encode('data: {"n":2}\n\n'));
  assert.deepEqual((await events.next()).value, { n: 2 });
  controller.close();
  assert.equal((await events.next()).done, true);
  assert.equal(body.locked, false);
});

test("malformed JSON and empty data fail clearly and release the reader", async () => {
  for (const text of ["data: broken\n\n", "data:\n\n", "data: {\n"]) {
    const body = bytes([encoder.encode(text)]);
    await assert.rejects(collect(parseSSE(body)), StreamParseError);
    assert.equal(body.locked, false);
  }
});

test("invalid UTF-8 is reported as a transport failure", async () => {
  const fetch: typeof globalThis.fetch = async () => new Response(bytes([Uint8Array.of(0xff)]), {
    headers: { "Content-Type": "text/event-stream" },
  });
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), TransportError);
});

test("breaking iteration cancels the source, releases its lock and aborts fetch", async () => {
  let cancelled = 0;
  let signal: AbortSignal | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode("data: 1\n\n")); },
    cancel() { cancelled++; },
  });
  const fetch: typeof globalThis.fetch = async (_url, init) => {
    signal = init?.signal ?? undefined;
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  };
  for await (const event of stream<number>({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })) {
    assert.equal(event, 1);
    break;
  }
  assert.equal(cancelled, 1);
  assert.equal(body.locked, false);
  assert.equal(signal?.aborted, true);
});

test("AbortSignal interrupts a blocked reader and cleans up", async () => {
  let cancelled = false;
  let started!: () => void;
  const fetched = new Promise<void>(resolve => { started = resolve; });
  const body = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
  const fetch: typeof globalThis.fetch = async () => {
    started();
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  };
  const controller = new AbortController();
  const result = collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" }, { signal: controller.signal }));
  await fetched;
  controller.abort();
  await assert.rejects(result, { name: "AbortError" });
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});

test("already aborted signals never fetch", async () => {
  let calls = 0;
  const fetch: typeof globalThis.fetch = async () => { calls++; return response(""); };
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" }, { signal: AbortSignal.abort() })), { name: "AbortError" });
  assert.equal(calls, 0);
});

test("HTTP errors contain status, headers and JSON or text details", async () => {
  for (const [raw, details] of [[JSON.stringify({ detail: "Denied" }), { detail: "Denied" }], ["Proxy failed", "Proxy failed"]] as const) {
    const fetch: typeof globalThis.fetch = async () => new Response(raw, { status: 403, headers: { "X-Error": "test" } });
    await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), (error: unknown) => {
      assert.ok(error instanceof HttpError);
      assert.equal(error.status, 403);
      assert.deepEqual(error.details, details);
      assert.equal(error.response.headers.get("X-Error"), "test");
      return true;
    });
  }
});

test("error response diagnostics are bounded", async () => {
  const fetch: typeof globalThis.fetch = async () => new Response("x".repeat(100000), { status: 502 });
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), (error: unknown) => {
    assert.ok(error instanceof HttpError);
    assert.equal((error.details as string).length, 65536);
    return true;
  });
});

test("wrong content types, empty bodies and network failures are transport errors", async () => {
  const implementations: (typeof globalThis.fetch)[] = [
    async () => new Response("html"),
    async () => new Response(null, { headers: { "Content-Type": "text/event-stream" } }),
    async () => { throw new TypeError("offline"); },
  ];
  for (const fetch of implementations) {
    await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), TransportError);
  }
});

test("a broken network stream preserves the transport error type", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.error(new Error("disconnected")); },
  });
  const fetch: typeof globalThis.fetch = async () => new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
  });
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), TransportError);
  assert.equal(body.locked, false);
});

test("a failed HTTP error body preserves its known status", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.error(new Error("error body disconnected")); },
  });
  const fetch: typeof globalThis.fetch = async () => new Response(body, { status: 502 });
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" })), (error: unknown) => {
    assert.ok(error instanceof HttpError);
    assert.equal(error.status, 502);
    assert.ok(error.cause instanceof Error);
    return true;
  });
});

test("generated GET/POST methods serialize input and merge headers", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetch: typeof globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init: init! });
    return response('data: {"type":"run.completed","run_id":"abc"}\n\n');
  };
  const client = createClient({ baseUrl: "http://example.test/api/", headers: { Authorization: "Bearer default", "X-Client": "sdk" }, fetch });
  await collect(client.agentRuns.streamRunEvents({ runId: "a/b ?", delay: 0 }, { headers: { Authorization: "Bearer override" } }));
  await collect(client.agentRuns.streamPrompt({ runId: "abc", body: { prompt: "Hello" } }));
  assert.equal(calls[0]?.url, "http://example.test/api/v1/runs/a%2Fb%20%3F/events?delay=0");
  assert.equal(calls[0]?.init.method, "GET");
  assert.equal(new Headers(calls[0]?.init.headers).get("Authorization"), "Bearer override");
  assert.equal(new Headers(calls[0]?.init.headers).get("X-Client"), "sdk");
  assert.equal(calls[1]?.init.method, "POST");
  assert.equal(calls[1]?.init.body, '{"prompt":"Hello"}');
  assert.equal(new Headers(calls[1]?.init.headers).get("Content-Type"), "application/json");
  assert.throws(() => client.agentRuns.streamRunEvents({ runId: ".." }), /dot segments/);
});

test("query arrays preserve explode semantics and optional header values", () => {
  const query = new URLSearchParams();
  appendQuery(query, "repeated", ["a b", "c"], true);
  appendQuery(query, "joined", [1, 2], false);
  appendQuery(query, "missing", undefined, true);
  assert.equal(query.toString(), "repeated=a+b&repeated=c&joined=1%2C2");
  const headers = new Headers();
  setHeader(headers, "x-trace", "trace");
  setHeader(headers, "x-missing", undefined);
  assert.equal(headers.get("x-trace"), "trace");
  assert.equal(headers.has("x-missing"), false);
});

test("optional scalar null is omitted while actual scalar and array values survive", () => {
  const query = new URLSearchParams();
  const headers = new Headers();
  for (const value of [undefined, null]) {
    appendQuery(query, "missing", value, true);
    setHeader(headers, "x-missing", value);
  }
  assert.equal(query.has("missing"), false);
  assert.equal(headers.has("x-missing"), false);
  for (const value of ["null", false, 0, ""]) {
    appendQuery(query, "value", value, true);
    setHeader(headers, "x-value", value);
    assert.equal(headers.get("x-value"), String(value));
  }
  assert.deepEqual(query.getAll("value"), ["null", "false", "0", ""]);
  appendQuery(query, "array", [null, "null", false, 0, ""], true);
  appendQuery(query, "joined", [null, "null", false, 0, ""], false);
  assert.deepEqual(query.getAll("array"), ["null", "null", "false", "0", ""]);
  assert.equal(query.get("joined"), "null,null,false,0,");
});

test("required query and header nulls fail before serialization", () => {
  for (const value of [null, undefined]) {
    assert.throws(() => appendQuery(new URLSearchParams(), "limit", value, true, true), /Required query parameter limit/);
    assert.throws(() => setHeader(new Headers(), "x-limit", value, true), /Required header parameter x-limit/);
  }
});

test("unknownEvents ignore skips only objects with undeclared string tags", async () => {
  const body = [
    'data: {"type":"run.started","run_id":"a"}',
    'data: {"type":"run.paused"}',
    'data: {"type":7}',
    'data: {"other":true}',
    "data: [1]",
    'data: {"type":"run.completed","run_id":"a"}',
  ].join("\n\n");
  const fetch = async () => response(body);
  const client = { baseUrl: "http://example.test", fetch };
  const request = { path: "/", method: "GET", events: { discriminator: "type", known: ["run.completed", "run.started"] } };
  assert.equal((await collect(stream(client, request))).length, 6);
  assert.deepEqual(await collect(stream(client, request, { unknownEvents: "ignore" })), [
    { type: "run.started", run_id: "a" },
    { type: 7 },
    { other: true },
    [1],
    { type: "run.completed", run_id: "a" },
  ]);
});

test("unknownEvents ignore still fails on invalid JSON", async () => {
  const fetch = async () => response('data: {"type":"run.paused"}\n\ndata: {oops\n\n');
  const request = { path: "/", method: "GET", events: { discriminator: "type", known: [] } };
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, request, { unknownEvents: "ignore" })), (error: unknown) => {
    assert.ok(error instanceof StreamParseError);
    return true;
  });
});

test("unknownEvents ignore requires a discriminated union before any request", async () => {
  let calls = 0;
  const fetch = async () => {
    calls++;
    return response("");
  };
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" }, { unknownEvents: "ignore" })), TypeError);
  await assert.rejects(collect(stream({ baseUrl: "http://example.test", fetch }, { path: "/", method: "GET" }, { unknownEvents: "drop" as "ignore" })), TypeError);
  assert.equal(calls, 0);
});

test("generated operations declare their known event tags", async () => {
  const body = 'data: {"type":"run.paused"}\n\ndata: {"type":"run.completed","run_id":"run_abc"}\n\n';
  const fetch = async () => response(body);
  const client = createClient({ baseUrl: "http://example.test", fetch });
  const events = await collect(client.agentRuns.streamRunEvents({ runId: "run_abc" }, { unknownEvents: "ignore" }));
  assert.deepEqual(events, [{ type: "run.completed", run_id: "run_abc" }]);
});
