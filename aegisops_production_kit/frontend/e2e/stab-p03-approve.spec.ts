import { type Browser, expect, type Page, test } from "@playwright/test";

// STAB P0-3 — single-user HITL approve UX (initiator == approver is THE approval model):
//  1. A same-user REJECT closes the run honestly and visibly — "Rejected — nothing was
//     changed." (no phantom applying strip, no silence).
//  2. The two-user flow — dev.engineer (non-approver role) initiates, maya approves from a
//     FRESHLY OPENED session (the restored approval card: without it no approval was
//     possible from a window that never saw the live stream) — flips instantly, streams
//     live per-step progress mid-apply, and lands the applied state. This gate is RBAC
//     (approver role), not a second-approver policy: maya could equally approve her own run.
//  3. The gated destroy streams the same live progress (cleanup — no cloud residue).
//
// LIVE spec (real AWS apply + destroy of a throwaway S3 bucket) — run on demand inside a
// valid cred window, excluded from routine canaries like STAB P0-2:
//   npx playwright test e2e/stab-p03-approve.spec.ts --project=chromium --workers=1

const STAMP = `${Date.now() % 1000000}`;
const BUCKET = `stab-p03-${STAMP}`;

test.describe.configure({ mode: "serial" });

async function loginAs(page: Page, email: string, password: string) {
  await page.goto("/");
  await expect(page.getByText("Sign in to AegisOps")).toBeVisible({ timeout: 15000 });
  const inputs = page.locator("input");
  await inputs.first().fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
}

async function sendWhenReady(page: Page, text: string) {
  const box = page.getByPlaceholder(/Ask AegisOps/);
  await box.fill(text);
  for (let i = 0; i < 60; i++) {
    await box.press("Enter");
    try {
      await expect(box).toHaveValue("", { timeout: 2000 });
      return;
    } catch { /* previous turn still streaming (P1-6) — retry */ }
  }
  throw new Error("composer never accepted the message");
}

async function approveAndWatch(page: Page) {
  const approve = page.getByRole("button", { name: "Approve & apply" });
  await expect(approve.last()).toBeVisible({ timeout: 240000 });
  await approve.last().click();

  // 1. INSTANT flip — the applying strip replaces the decision card within seconds.
  const strip = page.getByText("Approved — applying now").last();
  await expect(strip).toBeVisible({ timeout: 3000 });
  await expect(page.getByRole("button", { name: "Approve & apply" })).toHaveCount(0);

  // 2. LIVE mid-apply progress: the strip's step line CHANGES while the apply runs.
  const sub = page.locator("text=/Step \\d+: |Starting the apply/").last();
  const initial = (await sub.textContent().catch(() => "")) ?? "";
  await expect(async () => {
    const now = (await sub.textContent().catch(() => "")) ?? "";
    expect(now).not.toBe(initial);
  }).toPass({ timeout: 180000 });

  // 3. Completion: strip retires, the Terraform card states the terminal fact.
  await expect(page.getByText("Approved — applying now")).toHaveCount(0, { timeout: 240000 });
  await expect(page.getByText(/· applied/).last()).toBeVisible({ timeout: 10000 });
}

async function initiateBucketAsk(page: Page, message: string, expectParamsAsk = true) {
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 15000 });
  await sendWhenReady(page, message);
  if (expectParamsAsk) {
    await expect(page.getByText(/Bucket name/).first()).toBeVisible({ timeout: 180000 });
  }
}

test("a same-user REJECT closes the run honestly and visibly (single-user HITL)", async ({ page }) => {
  test.setTimeout(420000);
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  await initiateBucketAsk(page, "Provision an S3 bucket in AWS us-east-1");
  await sendWhenReady(page, `stab-p03d-${STAMP}`);

  const reject = page.getByRole("button", { name: "Reject" });
  await expect(reject.last()).toBeVisible({ timeout: 240000 });
  await reject.last().click();

  // The rejection is VISIBLE and honest — nothing executed, no phantom applying strip…
  await expect(page.getByText("Rejected — nothing was changed.").last()).toBeVisible({ timeout: 30000 });
  // …and the decision card is retired: the run is closed, not waiting for anyone else.
  await expect(page.getByRole("button", { name: "Approve & apply" })).toHaveCount(0, { timeout: 10000 });
});

async function twoUserApprove(browser: Browser, initiate: (dev: Page) => Promise<void>, sessionTitle: RegExp) {
  const devCtx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const dev = await devCtx.newPage();
  await loginAs(dev, "dev.engineer@northwind.com", "aegisops");
  await initiate(dev);
  // The initiator (devops-engineer) sees the gate but cannot approve — honest RBAC render.
  await expect(dev.getByText("Approver role required").last()).toBeVisible({ timeout: 240000 });

  const mayaCtx = await browser.newContext({ storageState: "e2e/.auth/user.json" });
  const maya = await mayaCtx.newPage();
  await maya.goto("/");
  await expect(maya.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  // Open the freshly-created session from the org sidebar — the approval card must be
  // RESTORED from the awaiting run (this window never saw the live interrupt stream).
  await maya.getByText(sessionTitle).first().click();
  await approveAndWatch(maya);
  await devCtx.close();
  await mayaCtx.close();
  return maya;
}

test("two-user flow: initiate as dev.engineer (non-approver), approve as maya → live progress → applied", async ({ browser }) => {
  test.setTimeout(600000);
  // The bucket name IS the opener's unique token: one turn straight to the plan card,
  // and maya's sidebar click can never land on a residual same-titled session.
  await twoUserApprove(browser, async (dev) => {
    await initiateBucketAsk(dev, `Provision an S3 bucket named ${BUCKET} in AWS us-east-1`, false);
  }, new RegExp(BUCKET));
});

test("cleanup: the gated destroy streams the same live progress", async ({ browser }) => {
  test.setTimeout(600000);
  await twoUserApprove(browser, async (dev) => {
    await initiateBucketAsk(dev, `destroy ${BUCKET}`, false);
  }, new RegExp(`destroy ${BUCKET}`));
});
