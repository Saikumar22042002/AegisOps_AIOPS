import { expect, type Page, test } from "@playwright/test";

// STAB P0-2 — fresh-stack retest of the DEP VPC-ask convergence with the EXACT live
// phrasings from Screenshots/1.png + 2.png (2026-07-13). Two-turn flow through the real
// UI: turn 1 is a fully-parameterized EC2 create (params complete → the DEP ask fires
// when ≥2 active aws.vpc candidates exist), turn 2 is the reply phrasing under test.
//
// PASS = the reply maps onto the asked slot ("Placement answered · vpc → …" step emitted
// by cloudops) — the ask is never repeated. FAIL (the screenshot bug) = the identical ask
// re-appears and no placement step ever arrives, so the marker times out.
//
// Precondition (documented in docs/STAB_MATRIX.md): the INV-HON-marked rows
// accept-ec2-net + accept-web3-net are temporarily restored to active
// (`python -m app.admin mark-unreachable <name> --undo`) so the ask offers real
// candidates; they are re-marked unreachable after the retest.

const ASK = /Which vpc should this use/i;
const PLACEMENT = /Placement answered/i;

async function openFreshConversation(page: Page) {
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 15000 });
}

async function twoTurn(page: Page, name: string, reply: string) {
  test.setTimeout(240000); // two live-LLM turns + the plan trigger comfortably exceed the 30s default
  await openFreshConversation(page);
  const box = page.getByPlaceholder(/Ask AegisOps/);

  // Turn 1 — params-complete create, so the next question can only be the DEP ask.
  await box.fill(`create an aws ec2 instance named ${name}, t3.micro, amazon-linux-2023, create a key, no remote access`);
  await box.press("Enter");
  await expect(page.getByText(ASK).first()).toBeVisible({ timeout: 180000 });

  // Turn 2 — the phrasing under test. sendText silently no-ops while the previous turn is
  // still streaming (store.ts guard), so press Enter until the composer actually clears —
  // the accepted-send signal (sendText sets input:"" only when it runs).
  await box.fill(reply);
  let sent = false;
  for (let i = 0; i < 60 && !sent; i++) {
    await box.press("Enter");
    try {
      await expect(box).toHaveValue("", { timeout: 2000 });
      sent = true;
    } catch { /* previous turn still streaming — retry */ }
  }
  expect(sent, "composer never accepted the reply — previous turn never finished streaming").toBe(true);

  // Convergence marker: the DEP mapper ran on the asked slot. If the live bug were still
  // present, the identical ask streams again instead and this times out.
  await expect(page.getByText(PLACEMENT).first()).toBeVisible({ timeout: 180000 });
}

test.describe("STAB P0-2 · DEP ask converges across turns (exact live phrasings)", () => {
  test('reply "use accept-ec2-net" (screenshot 1)', async ({ page }) => {
    await twoTurn(page, `stab-p02a-${Date.now() % 100000}`, "use accept-ec2-net");
  });

  test('reply «use this accept-ec2-net" (vpc-0f411efc6ab891632)» (screenshot 2)', async ({ page }) => {
    await twoTurn(page, `stab-p02b-${Date.now() % 100000}`,
      'use this accept-ec2-net" (vpc-0f411efc6ab891632)');
  });

  test('reply "new" → create-first DAG, never a re-ask', async ({ page }) => {
    await twoTurn(page, `stab-p02c-${Date.now() % 100000}`, "new");
  });
});
