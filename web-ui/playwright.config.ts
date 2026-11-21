import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.ts",
  outputDir: "../.artifacts/e2e/playwright-results",
  reporter: [["list"], ["junit", { outputFile: "../.artifacts/e2e/playwright-junit.xml" }]],
  use: {
    baseURL: process.env.MUNINN_WEB_BASE_URL ?? "http://127.0.0.1:4175",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      testIgnore: "**/tests/integration/**",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
        },
      },
    },
    {
      name: "integration",
      testMatch: "**/tests/integration/**",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
        },
      },
    },
  ],
  webServer: process.env.MUNINN_WEB_BASE_URL
    ? undefined
    : {
        command: `corepack pnpm dev ${process.env.MUNINN_OIDC_INTEGRATION === "1" ? "" : "--mode test"} --host 127.0.0.1 --port 4175`,
        url: "http://127.0.0.1:4175",
        reuseExistingServer: false,
      },
});
