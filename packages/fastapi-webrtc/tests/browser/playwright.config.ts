import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.FASTAPI_WEBRTC_BROWSER_PORT ?? 8765);
// The checks script passes the workspace interpreter; standalone runs use PATH.
const python = process.env.FASTAPI_WEBRTC_PYTHON ?? "python";

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  outputDir: "results",
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        permissions: ["microphone", "camera"],
        launchOptions: {
          args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        },
      },
    },
  ],
  webServer: {
    command: `"${python}" -m tests.browser.server`,
    cwd: "../..",
    env: { PORT: String(port) },
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
