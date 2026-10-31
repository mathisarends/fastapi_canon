import assert from "node:assert/strict";
import { createClient, HttpError } from "../../examples/agent_runs/generated/index.js";
import type { RunEvent } from "../../examples/agent_runs/generated/types.js";

const client = createClient({ baseUrl: process.argv[2]!, headers: { Authorization: "Bearer demo-token" } });
const events: RunEvent[] = [];
for await (const event of client.agentRuns.streamRunEvents({ runId: "e2e", delay: 0.001 })) {
  events.push(event);
}
assert.equal(events[0]?.type, "run.started");
assert.equal(events.at(-1)?.type, "run.completed");
assert.equal(events.filter(event => event.type === "token.delta").map(event => event.text).join(""), "Hello 世界 👋");
const posted: RunEvent[] = [];
for await (const event of client.agentRuns.streamPrompt({ runId: "post", body: { prompt: "typed streams" } })) posted.push(event);
assert.equal(posted.filter(event => event.type === "token.delta").length, 2);
const anonymous = createClient({ baseUrl: process.argv[2]! });
try {
  for await (const event of anonymous.agentRuns.streamPrompt({ runId: "post", body: { prompt: "denied" } })) {
    assert.fail(`Unexpected event ${event.type}`);
  }
  assert.fail("Expected authentication to fail");
} catch (error) {
  assert.ok(error instanceof HttpError);
  assert.ok(error.status === 401 || error.status === 403);
}
console.log("Pydantic -> FastAPI -> OpenAPI -> generated Fetch client: passed");
