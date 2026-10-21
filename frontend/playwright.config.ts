import { existsSync } from "node:fs";
import { defineConfig } from "@playwright/test";

const API = "http://127.0.0.1:8788";
const WEB = "http://127.0.0.1:4188";

// Use a system Chromium when one is provided (CI images, containers); otherwise Playwright's own.
const candidates = [process.env.CHROMIUM_PATH, "/opt/pw-browsers/chromium"].filter(Boolean) as string[];
const executablePath = candidates.find((p) => existsSync(p));

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: WEB,
    trace: "retain-on-failure",
    acceptDownloads: true,
    launchOptions: { executablePath, args: ["--no-sandbox"] },
  },
  webServer: [
    {
      // The real backend: PostgreSQL, Redis, the Dramatiq worker and the API, on the offline mock.
      command: "bash e2e/stack/run.sh",
      url: `${API}/readyz`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      // The production build of the dashboard, proxying /api to that backend.
      command: "npx vite build && npx vite preview --port 4188 --strictPort",
      url: WEB,
      timeout: 120_000,
      reuseExistingServer: false,
      env: { API_URL: API },
    },
  ],
});
