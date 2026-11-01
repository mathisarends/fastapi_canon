import { createClient, RunEventType } from "./generated/index.js";

const client = createClient({
  baseUrl: "http://127.0.0.1:8000",
  headers: { Authorization: "Bearer demo-token" },
});

for await (const event of client.agentRuns.streamRunEvents({ runId: "demo" })) {
  switch (event.type) {
    case RunEventType.RUN_STARTED:
      console.log(`Started ${event.run_id}`);
      break;
    case RunEventType.TOKEN_DELTA:
      process.stdout.write(event.text);
      break;
    case RunEventType.RUN_COMPLETED:
      console.log(`\nCompleted ${event.run_id}`);
      break;
    case RunEventType.RUN_FAILED:
      console.error(event.message);
      break;
    default: {
      const exhaustive: never = event;
      throw new Error(`Unexpected event: ${exhaustive}`);
    }
  }
}
