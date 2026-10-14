import { defineConfig, devices } from "@playwright/test";
const external = process.env.SCHEMASHIELD_E2E_EXTERNAL === "1";
const runtime = process.env.SCHEMASHIELD_E2E_RUNTIME === "1";
if (runtime && !external)
  throw new Error(
    "Runtime browser tests require SCHEMASHIELD_E2E_EXTERNAL=1 and real PostgreSQL services.",
  );
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  retries: 0,
  timeout: 30_000,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.SCHEMASHIELD_E2E_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: external
    ? undefined
    : [
        {
          command:
            "uv run uvicorn schemashield.main:app --host 127.0.0.1 --port 8000",
          cwd: "..",
          url: "http://127.0.0.1:8000/health",
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
          env: {
            SCHEMASHIELD_DATABASE_URL: "sqlite:///.browser-tests.db",
            SCHEMASHIELD_ALLOW_SQLITE: "true",
            SCHEMASHIELD_API_TOKEN: "local-development-token-change-me",
            SCHEMASHIELD_SANDBOX_ENABLED: "false",
            SCHEMASHIELD_CORS_ORIGIN: "http://localhost:3000",
          },
        },
        {
          command: "npm run dev",
          url: "http://localhost:3000",
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      ],
});
