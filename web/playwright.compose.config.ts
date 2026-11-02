import { defineConfig, devices } from "@playwright/test";

// Real auth tests must never save credentials in tracing or error snapshots.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";

export default defineConfig({
  testDir: "./compose-tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: "line",
  outputDir: "./test-results/compose",
  use: {
    baseURL: "http://127.0.0.1:8088",
    actionTimeout: 15_000,
    navigationTimeout: 15_000,
    trace: "off", screenshot: "off", video: "off",
  },
  projects: [{ name: "chromium-compose", use: { ...devices["Desktop Chrome"] } }],
});
