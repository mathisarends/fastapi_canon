import assert from "node:assert/strict";
import { test } from "node:test";
import { createClient } from "../fixtures/generated/index.js";
import { MetadataMode } from "../fixtures/generated/index.js";
import type { FixtureEvent, NestedEvent } from "../fixtures/generated/types.js";

const nested: NestedEvent = {
  type: "nested",
  "external-id": "1",
  metadata: { labels: ["typed"], limits: { max: 2 }, note: null, mode: MetadataMode.FAST },
  children: [],
};

export function narrow(event: FixtureEvent): string | number {
  switch (event.type) {
    case "nested": return event["external-id"];
    case "done": return event.count;
    default: {
      const never: never = event;
      return never;
    }
  }
}

test("nested models, recursive arrays, nullable fields, aliases and typed parameters", async () => {
  const fetch: typeof globalThis.fetch = async (url, init) => {
    assert.equal(String(url), "http://example.test/nested/1?labels=a&labels=b");
    assert.equal(new Headers(init?.headers).get("x-trace"), "trace");
    return new Response(`data: ${JSON.stringify(nested)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
  };
  const client = createClient({ baseUrl: "http://example.test", fetch });
  for await (const event of client.fixtures.nestedEvents({ itemId: 1, labels: ["a", "b"], xTrace: "trace" })) {
    assert.deepEqual(event, nested);
    assert.equal(narrow(event), "1");
  }
});
