import { expect, type Page, test } from "@playwright/test";

type Scenario = "practice" | "denied" | "clock" | "renegotiate";
type Scenarios = Record<Scenario, () => Promise<Record<string, unknown>>>;

async function run(page: Page, scenario: Scenario): Promise<Record<string, unknown>> {
  await page.goto("/");
  await page.waitForSelector("body[data-ready=true]", { state: "attached" });
  return page.evaluate((name) => (window as unknown as { scenarios: Scenarios }).scenarios[name](), scenario);
}

test("practice echoes the microphone and answers typed prompts", async ({ page }) => {
  const result = await run(page, "practice");
  expect(result.remoteKinds).toEqual(["audio"]);
  const [feedback] = result.feedback as { text: string; audio_frames: number }[];
  expect(feedback?.text).toBe("You said: hello tutor");
  expect(feedback?.audio_frames).toBeGreaterThan(0);
  expect(result.inboundAudio).toBeGreaterThan(0);
  expect(result.reason).toBe("closed");
  expect(result.state).toBe("closed");
  expect(result.microphoneLive).toBe(true);
});

test("dependency admission denies a wrong token before negotiation", async ({ page }) => {
  expect(await run(page, "denied")).toEqual({ code: "connection_failed" });
});

test("server-offered sessions deliver video and typed ticks", async ({ page }) => {
  const result = await run(page, "clock");
  expect(result.ticks).toEqual([0, 1, 2]);
  expect(result.width).toBeGreaterThan(0);
  expect(result.state).toBe("closed");
  expect(result.measurement).toBe("presented");
  expect(result.reasons).toEqual(["first_frame", "stall_timeout", "recovered", "stopped"]);
  expect(result.stalledSessionOpen).toBe(true);
});

test("adding a track renegotiates an open session", async ({ page }) => {
  expect(await run(page, "renegotiate")).toEqual({ remote: 1, live: true });
});
