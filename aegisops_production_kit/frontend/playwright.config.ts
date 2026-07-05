import { defineConfig, devices } from "@playwright/test";

// E2E runs against a running app (`make dev` / `docker compose --profile full up`).
// Auth is established ONCE by the `setup` project (real Keycloak password grant) and the session
// cookie is reused via storageState by the browser projects — so we don't stampede the login rate
// limiter with a fresh grant per test. Tests that must be unauthenticated clear storageState.
const STORAGE = "e2e/.auth/user.json";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    { name: "chromium", use: { ...devices["Desktop Chrome"], storageState: STORAGE }, dependencies: ["setup"] },
    { name: "mobile", use: { ...devices["Pixel 7"], storageState: STORAGE }, dependencies: ["setup"] },
  ],
});
