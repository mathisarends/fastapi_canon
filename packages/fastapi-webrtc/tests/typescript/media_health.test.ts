import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import {
  observeVideoHealth,
  type VideoHealthEvent,
  type VideoHealthSource,
} from "../../examples/practice/client/index.js";
import { flush } from "./fakes.js";

function report(...rows: Record<string, unknown>[]): RTCStatsReport {
  return new Map(rows.map((row, index) => [String(row.id ?? index), row])) as unknown as RTCStatsReport;
}

function video(count?: number, id = "video", track = "track"): RTCStatsReport {
  return report({ id, type: "inbound-rtp", kind: "video", trackIdentifier: track, framesDecoded: count });
}

class Source implements VideoHealthSource {
  value = video(0);
  calls = 0;
  failure: Error | null = null;
  pending: Promise<RTCStatsReport> | null = null;
  close!: () => void;
  closed = new Promise<void>((resolve) => { this.close = resolve; });
  async stats(): Promise<RTCStatsReport> {
    this.calls += 1;
    if (this.failure !== null) throw this.failure;
    return this.pending ?? this.value;
  }
}

async function tick(t: TestContext, duration: number): Promise<void> {
  t.mock.timers.tick(duration);
  await flush(2);
}

test("first-frame and stall deadlines are distinct, recoverable observations", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  const events: VideoHealthEvent[] = [];
  const observer = observeVideoHealth(source, {
    firstFrameTimeoutMs: 100, stallTimeoutMs: 50, pollIntervalMs: 10,
    onChange: (event) => events.push(event),
  });
  await flush(2);
  assert.equal(observer.state, "waiting");
  await tick(t, 100);
  assert.equal(events.at(-1)?.reason, "first_frame_timeout");
  source.value = video(1);
  await tick(t, 10);
  assert.equal(observer.state, "healthy");
  assert.equal(events.at(-1)?.reason, "first_frame");
  await tick(t, 50);
  assert.equal(events.at(-1)?.reason, "stall_timeout");
  source.value = video(2);
  await tick(t, 10);
  assert.equal(events.at(-1)?.reason, "recovered");
  observer.stop();
  observer.stop();
  assert.equal(observer.state, "stopped");
  assert.equal(events.filter((event) => event.reason === "stopped").length, 1);
});

test("unavailable metrics and other tracks do not count as video progress", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  source.value = report(
    { id: "out", type: "outbound-rtp", kind: "video", framesDecoded: 9 },
    { id: "audio", type: "inbound-rtp", kind: "audio", framesDecoded: 9 },
    { id: "other", type: "inbound-rtp", kind: "video", trackIdentifier: "other", framesDecoded: 9 },
    { id: "nan", type: "inbound-rtp", kind: "video", trackIdentifier: "track", framesDecoded: NaN },
    { id: "negative", type: "inbound-rtp", kind: "video", trackIdentifier: "track", framesDecoded: -1 },
    { id: "missing", type: "inbound-rtp", kind: "video", trackIdentifier: "track" },
  );
  const observer = observeVideoHealth(source, { trackId: "track", pollIntervalMs: 10 });
  await flush(2);
  await tick(t, 100_000);
  assert.equal(observer.state, "waiting"); // Deadlines are opt-in.
  source.value = video(3);
  await tick(t, 10);
  assert.equal(observer.state, "healthy");
  observer.stop();
});

test("counter reset, SSRC replacement and advancing frames refresh the stall deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  source.value = video(5);
  const observer = observeVideoHealth(source, { stallTimeoutMs: 50, pollIntervalMs: 10 });
  await flush(2);
  for (const value of [video(6), video(1), video(1, "replacement")]) {
    source.value = value;
    await tick(t, 40);
    assert.equal(observer.state, "healthy");
  }
  source.value = video(0, "replacement");
  await tick(t, 40);
  await tick(t, 10);
  assert.equal(observer.state, "stalled");
  observer.stop();
});

test("slow stats calls do not overlap or postpone the first-frame deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  let resolve!: (value: RTCStatsReport) => void;
  source.pending = new Promise((done) => { resolve = done; });
  const events: VideoHealthEvent[] = [];
  const observer = observeVideoHealth(source, {
    firstFrameTimeoutMs: 20, pollIntervalMs: 1, onChange: (event) => events.push(event),
  });
  await tick(t, 20);
  assert.equal(observer.state, "stalled");
  assert.equal(source.calls, 1);
  observer.stop();
  resolve(video(1));
  await flush(2);
  await tick(t, 100);
  assert.equal(source.calls, 1);
  assert.deepEqual(events.map((event) => event.state), ["stalled", "stopped"]);
});

test("a temporarily absent stats sample cannot manufacture frame progress", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  source.value = video(1);
  const observer = observeVideoHealth(source, { stallTimeoutMs: 50, pollIntervalMs: 10 });
  await flush(2);
  source.value = report();
  await tick(t, 20);
  source.value = video(1);
  await tick(t, 20);
  await tick(t, 10);
  assert.equal(observer.state, "stalled");
  observer.stop();
});

test("stats and consumer callback errors stay observational", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const source = new Source();
  source.failure = new Error("unavailable");
  const errors: unknown[] = [];
  const observer = observeVideoHealth(source, {
    pollIntervalMs: 10,
    onError: (error) => { errors.push(error); throw new Error("callback"); },
    onChange: () => { throw new Error("change"); },
  });
  await flush(2);
  assert.equal(errors[0], source.failure);
  source.failure = null;
  source.value = video(1);
  await tick(t, 10);
  assert.equal(observer.state, "healthy");
  assert.equal(errors.length, 2);
  observer.stop();
});

test("source close and abort stop polling, including an already aborted signal", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const mode of ["close", "abort", "already_aborted"]) {
    const source = new Source();
    const controller = new AbortController();
    if (mode === "already_aborted") controller.abort();
    const observer = observeVideoHealth(source, { signal: controller.signal });
    if (mode === "close") source.close();
    else controller.abort();
    await flush(2);
    const calls = source.calls;
    await tick(t, 10_000);
    assert.equal(observer.state, "stopped");
    assert.equal(source.calls, calls);
    if (mode === "already_aborted") assert.equal(calls, 0);
  }
  const source = { stats: async () => report(), closed: Promise.reject(new Error("closed")) };
  const observer = observeVideoHealth(source);
  await flush(2);
  assert.equal(observer.state, "stopped");
});

test("video frame callbacks observe compositor submission and are cancelled", () => {
  const source = new Source();
  const callbacks: VideoFrameRequestCallback[] = [];
  const cancelled: number[] = [];
  const element = {
    requestVideoFrameCallback(callback: VideoFrameRequestCallback): number { callbacks.push(callback); return callbacks.length; },
    cancelVideoFrameCallback(id: number): void { cancelled.push(id); },
  } as HTMLVideoElement;
  const events: VideoHealthEvent[] = [];
  const observer = observeVideoHealth(source, { video: element, onChange: (event) => events.push(event) });
  callbacks[0]?.(0, {} as VideoFrameCallbackMetadata);
  assert.equal(observer.state, "healthy");
  assert.equal(events[0]?.measurement, "presented");
  assert.equal(source.calls, 0);
  observer.stop();
  assert.deepEqual(cancelled, [2]);
  callbacks[1]?.(0, {} as VideoFrameCallbackMetadata);
  assert.equal(callbacks.length, 2);
});

test("missing or failing video callbacks fall back to decoded stats", async () => {
  for (const element of [
    {} as HTMLVideoElement,
    { requestVideoFrameCallback(): number { throw new Error("unsupported"); } } as unknown as HTMLVideoElement,
  ]) {
    const source = new Source();
    source.value = video(1);
    const events: VideoHealthEvent[] = [];
    const observer = observeVideoHealth(source, { video: element, onChange: (event) => events.push(event) });
    await flush(2);
    assert.equal(events[0]?.measurement, "decoded");
    observer.stop();
  }
});

test("invalid durations fail before observation starts", () => {
  const source = new Source();
  for (const value of [0, -1, NaN, Infinity, 2_147_483_648]) {
    for (const name of ["firstFrameTimeoutMs", "stallTimeoutMs", "pollIntervalMs"]) {
      assert.throws(() => observeVideoHealth(source, { [name]: value }), RangeError);
    }
  }
  assert.equal(source.calls, 0);
});
