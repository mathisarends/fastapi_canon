// Invoked by benchmark_runtime.py against a freshly generated, compiled SDK.
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [sdk, payloadPath, eventArg, iterationArg, roundArg] = process.argv.slice(2);
const events = Number(eventArg);
const iterations = Number(iterationArg);
const rounds = Number(roundArg);
const { createClient } = await import(pathToFileURL(resolve(sdk, "index.js")).href);
const { parseSSE } = await import(pathToFileURL(resolve(sdk, "transport/sse.js")).href);
const payloads = JSON.parse(await readFile(payloadPath, "utf8"));
const encoder = new TextEncoder();
let activeChunks = [];
let sink;

function body() {
  return new ReadableStream({
    start(controller) {
      for (const chunk of activeChunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

const client = createClient({
  baseUrl: "http://benchmark.test",
  fetch: async () => new Response(body(), { headers: { "content-type": "text/event-stream" } }),
});

async function measure(operation, repeats, units) {
  for (let i = 0; i < 3; i++) await operation();
  const wall = [];
  const cpu = [];
  for (let round = 0; round < rounds; round++) {
    const started = process.hrtime.bigint();
    const cpuStarted = process.cpuUsage();
    for (let i = 0; i < repeats; i++) await operation();
    const elapsedCpu = process.cpuUsage(cpuStarted);
    cpu.push((elapsedCpu.user + elapsedCpu.system) / repeats / units);
    wall.push(Number(process.hrtime.bigint() - started) / repeats / units / 1000);
  }
  const median = values => {
    const sorted = [...values].sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  };
  return {
    iterations_per_round: repeats,
    units_per_iteration: units,
    median_us_per_unit: median(wall),
    cpu_median_us_per_unit: median(cpu),
    units_per_second: 1_000_000 / median(wall),
    samples_us_per_unit: wall,
  };
}

const results = {};
results.request_preparation = await measure(async () => {
  sink = client.bench.events({ runId: "run_abc", limit: 100 });
}, iterations * events, 1);

for (const name of ["small", "nested"]) {
  results[`decode_static_${name}`] = await measure(async () => {
    sink = JSON.parse(payloads[name]);
  }, iterations * events, 1);
}

for (const name of ["small", "nested", "fragmented"]) {
  const payload = payloads[name === "nested" ? "nested" : "small"];
  const raw = encoder.encode(`data: ${payload}\n\n`.repeat(events));
  activeChunks = name === "fragmented"
    ? Array.from({ length: Math.ceil(raw.length / 7) }, (_, index) => raw.subarray(index * 7, index * 7 + 7))
    : [raw];
  results[`stream_${name}`] = await measure(async () => {
    let received = 0;
    for await (const event of client.bench.events({ runId: "run_abc" })) {
      sink = event;
      received++;
    }
    if (received !== events) throw new Error("Incomplete benchmark stream");
  }, iterations, events);
  results[`frame_decode_${name}`] = await measure(async () => {
    for await (const event of parseSSE(body())) sink = event;
  }, iterations, events);
}

if (sink === undefined) throw new Error("Benchmark did not execute");
process.stdout.write(JSON.stringify({
  version: process.version,
  event_validation: "JSON decoding and static types; no runtime schema validation",
  results,
}));
