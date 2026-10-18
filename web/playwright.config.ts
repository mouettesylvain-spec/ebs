import { defineConfig, devices } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The e2e tests run the real metadata service (routes, authz, session cookies) on seeded,
// synthetic data (scripts/dashboard_e2e_server.py), serving the built dashboard at /ui/.
const port = Number(process.env.EBS_E2E_PORT ?? 8765);
const tmp = process.env.EBS_E2E_TMP ?? join(tmpdir(), `ebs-dashboard-e2e-${port}`);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1, // one seeded service; the live build test advances it
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["list"], ["junit", { outputFile: "e2e-junit.xml" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command:
      `npm run build && rm -rf ${tmp} && ` +
      `uv run --project .. python ../scripts/dashboard_e2e_server.py --port ${port} --dist dist --tmp ${tmp}`,
    url: `http://127.0.0.1:${port}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
