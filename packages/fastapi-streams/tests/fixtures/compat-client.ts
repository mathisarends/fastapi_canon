import { createClient } from "./generated/index.js";
import assert from "node:assert/strict";

const baseUrl = process.argv[2]!;
const client = createClient({ baseUrl });

async function state(id: string): Promise<string[]> {
  return (await fetch(`${baseUrl}/lifecycle/${id}`, { signal: AbortSignal.timeout(3000) })).json();
}

for (const mode of ["break", "abort"] as const) {
  const id = `typescript_${mode}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    const events = client.compat.openStream({ streamId: id }, { signal: controller.signal });
    if (mode === "break") {
      for await (const event of events) {
        if (event.n !== 1) throw new Error("Wrong first event");
        if ((await state(id)).length !== 1) throw new Error("Stream ended before consumption");
        break;
      }
    } else {
      const iterator = events[Symbol.asyncIterator]();
      if ((await iterator.next()).value?.n !== 1) throw new Error("Wrong first event");
      controller.abort();
      try {
        await iterator.next();
        throw new Error("Expected cancellation");
      } catch (error) {
        if (!(error instanceof Error) || error.name !== "AbortError") throw error;
      }
    }
    const deadline = Date.now() + 3000;
    while (true) {
      const closed = await state(id);
      if (closed.includes("generator_closed") && closed.includes("dependency_closed")) break;
      if (Date.now() > deadline) throw new Error("Server did not close stream and dependency");
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  } finally {
    clearTimeout(timer);
    controller.abort();
  }
}

for await (const event of client.fixtures.nestedEvents({ itemId: 42, labels: ["a b", "c&d"], xTrace: "trace" })) {
  if (event.type === "nested" && (event.metadata.labels.join("|") !== "a b|c&d" || event.metadata.note !== "trace")) {
    throw new Error("Wire values changed");
  }
}
for await (const event of client.compat.created()) {
  if (event.n !== 2) throw new Error("Custom success status failed");
}

for (const input of [{}, { limit: null, xLimit: null }, { limit: 2, xLimit: 3 }]) {
  for await (const event of client.compat.nullableParameters(input)) {
    if (event.n !== ((input.limit ?? 0) + (input.xLimit ?? 0))) throw new Error("Nullable parameter serialization failed");
  }
}
assert.throws(() => client.compat.requiredNullableParameters({ limit: null, xLimit: 1 }), /Required query parameter limit/);
assert.throws(() => client.compat.requiredNullableParameters({ limit: 1, xLimit: null }), /Required header parameter x-limit/);
for await (const event of client.compat.requiredNullableParameters({ limit: 2, xLimit: 3 })) {
  assert.equal(event.n, 5);
}
