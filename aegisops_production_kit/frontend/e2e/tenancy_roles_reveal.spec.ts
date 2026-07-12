import { expect, type Page, test } from "@playwright/test";

// Phase-1 UI-contract e2e (§5 flows 1–3), against the running app + real Keycloak:
//   1. Tenancy visible — an org-A user sees only org A; an org-B user sees only org B.
//   2. Roles honest — a read-only user gets the read-only composer notice, no send box.
//   3. Reveal step-up modal — Reveal → re-auth modal → wrong password stays with a re-auth
//      message → correct password shows the value once.
//
// Flows 1–2 are fully real (real logins, real /overview, real role gating). Flow 3 mocks the
// two responses a live cloud apply would produce (the completed-run SSE that carries a revealable
// credential, and the credential read) so the REAL browser modal + reveal flow are exercised
// without cloud credentials — the backend reveal contract itself is covered by
// backend tests/test_tenancy.py::TestCredentialRevealS1.

const NORTHWIND = "maya.okafor@northwind.com"; // org A · platform-admin (approver + initiator)
const ACME = "bob.chen@acme-industrial.com"; // org B · org-admin
const READONLY = "audit.viewer@northwind.com"; // org A · read-only

async function loginAs(page: Page, email: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByText("Sign in to AegisOps")).toBeVisible({ timeout: 20000 });
  await page.locator("input").first().fill(email);
  await page.locator('input[type="password"]').fill("aegisops");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Sign in to AegisOps")).toBeHidden({ timeout: 30000 });
}

test.describe("§5.1 tenancy visible in the UI", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("org-A user sees their organization (Northwind)", async ({ page }) => {
    await loginAs(page, NORTHWIND);
    await expect(page.getByText("Northwind Financial")).toBeVisible({ timeout: 20000 });
  });

  test("org-B user sees ONLY their organization (Acme), never org-A's", async ({ page }) => {
    await loginAs(page, ACME);
    // /overview resolves to org B — wait for its name to prove tenancy resolved server-side…
    await expect(page.getByText("Acme Industrial")).toBeVisible({ timeout: 20000 });
    // …and org-A's identity must never appear for an org-B principal.
    await expect(page.getByText("Northwind Financial")).toHaveCount(0);
  });
});

test.describe("§5.2 roles honest — read-only composer", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a read-only user sees the read-only notice and NO composer send", async ({ page }) => {
    await loginAs(page, READONLY);
    await expect(page.getByText(/view every conversation, run, and artifact/i)).toBeVisible({ timeout: 20000 });
    await expect(page.getByPlaceholder(/Ask AegisOps/)).toHaveCount(0);
  });

  test("an initiator sees the composer send box", async ({ page }) => {
    await loginAs(page, NORTHWIND);
    await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 20000 });
  });
});

test.describe("§5.3 credential reveal step-up modal", () => {
  // Reuses the maya storageState (authenticated). Mocks the completed-run SSE + the credential
  // read so the real reveal-button render path and step-up modal run in the browser.
  const RUN_ID = "11111111-1111-1111-1111-111111111111";
  const FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nE2E-STUBBED-KEY\n-----END PRIVATE KEY-----";

  test("Reveal → re-auth modal → wrong password stays → correct password shows the value once", async ({ page }) => {
    // 1. The chat SSE returns a completed run carrying a revealable sensitive output.
    await page.route("**/chat", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const frames =
        `event: run\ndata: ${JSON.stringify({ runId: RUN_ID, sessionId: "sess-e2e" })}\n\n` +
        `event: token\ndata: ${JSON.stringify({ text: "Instance ready." })}\n\n` +
        `event: done\ndata: ${JSON.stringify({
          messageId: "msg-e2e", runId: RUN_ID, traceId: RUN_ID,
          outcome: { status: "applied", sensitive_outputs: ["private_key_pem"] },
        })}\n\n`;
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: frames });
    });

    // 2. The credential read: first attempt 401 (stale/absent step-up), retry 200 with the value.
    let attempts = 0;
    await page.route("**/runs/*/credentials", async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({ status: 401, contentType: "application/json",
          body: JSON.stringify({ detail: "re-authenticate to reveal a credential" }) });
      } else {
        await route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ name: "private_key_pem", value: FAKE_KEY, one_time: true }) });
      }
    });

    await page.goto("/");
    const box = page.getByPlaceholder(/Ask AegisOps/);
    await expect(box).toBeVisible({ timeout: 30000 });
    await box.fill("create a VM in GCP");
    await box.press("Enter");

    // The revealable-credential card appears from the (mocked) completed run.
    const revealBtn = page.getByRole("button", { name: "Reveal credential" });
    await expect(revealBtn).toBeVisible({ timeout: 20000 });
    await revealBtn.click();

    // Step-up modal opens.
    await expect(page.getByText("Confirm it's you")).toBeVisible();
    const pw = page.locator('input[type="password"]');
    await pw.fill("wrong-first-try");
    await page.getByRole("button", { name: "Confirm & reveal" }).click();

    // 401 → the modal stays open with a re-auth message; the value is NOT shown.
    await expect(page.getByText(/didn't re-authenticate you/i)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText("E2E-STUBBED-KEY")).toHaveCount(0);

    // Correct re-auth → value shown once, modal closes.
    await pw.fill("aegisops");
    await page.getByRole("button", { name: "Confirm & reveal" }).click();
    await expect(page.getByText(/E2E-STUBBED-KEY/)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText("Confirm it's you")).toHaveCount(0);
    expect(attempts).toBe(2);
  });
});
