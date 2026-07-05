import { expect, type Page, test } from "@playwright/test";

// End-to-end core flow against the running app (make dev / compose full).
// Authenticated tests reuse the session cookie captured by auth.setup.ts (storageState). The
// unauthenticated group clears storageState to assert the real login screen. Both run on the
// `chromium` and `mobile` (Pixel 7) projects, so mobile responsive login+workspace is covered too.

test.describe("unauthenticated", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("login screen renders both auth paths", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Sign in to AegisOps")).toBeVisible();
    await expect(page.getByText("Continue with Keycloak SSO")).toBeVisible();
    await expect(page.getByText("SAML · MFA enforced · SOC 2 Type II")).toBeVisible();
  });

  test("theme toggle cycles Dark → Light on the login screen", async ({ page }) => {
    await page.goto("/");
    const dark = page.locator("button", { hasText: "Dark" }).first();
    await expect(dark).toBeVisible();
    await dark.click();
    await expect(page.locator("button", { hasText: "Light" }).first()).toBeVisible();
  });
});

test.describe("authenticated workspace", () => {
  async function openWorkspace(page: Page) {
    await page.goto("/");
    await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  }

  test("an authenticated session lands on the workspace", async ({ page }) => {
    await openWorkspace(page);
    // The composer + its approval-guard footer confirm the real workspace (not the login screen).
    await expect(page.getByText("Approval required")).toBeVisible();
  });

  test("sending a message streams a live run into the per-message timeline", async ({ page }) => {
    await openWorkspace(page);
    // Unique per run so the echoed user message is unambiguous even though the workspace restores
    // the persisted conversation (prior runs' messages remain in the thread).
    const q = `E2E pipeline check ${Date.now()}`;
    const box = page.getByPlaceholder(/Ask AegisOps/);
    await box.fill(q);
    await box.press("Enter");

    // The user's message is echoed + persisted…
    await expect(page.getByText(q)).toBeVisible();
    // …and the router emits its first step BEFORE any LLM call, so the newest message's live
    // "AI activity" timeline rendering proves the whole POST /chat SSE pipeline drives the UI
    // end-to-end (session → stream → per-message render).
    await expect(page.getByText("AI activity").last()).toBeVisible({ timeout: 30000 });

    // On desktop the artifact panel is docked and bound to a run (Timeline tab visible). On mobile
    // it's a collapsed drawer per the responsive design, so only assert it where it's docked.
    if ((page.viewportSize()?.width ?? 1200) >= 700) {
      await expect(page.getByText("Timeline", { exact: true }).first()).toBeVisible();
    }
  });
});
