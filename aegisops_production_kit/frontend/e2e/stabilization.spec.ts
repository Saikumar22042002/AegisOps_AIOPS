import { expect, type Page, test } from "@playwright/test";

// Phase-A E2E journeys (03_TEST_MATRIX §I) — real login (storageState from auth.setup.ts),
// real backend, real Gemini. The resource-creation invariants (create-two-coexist,
// destroy-only-named) run in the pytest live-cloud tier (test_safety_live.py) where teardown
// is deterministic; here we cover the conversational journeys the UI owns.

async function openWorkspace(page: Page) {
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
}

async function send(page: Page, text: string, turnNo: number) {
  const box = page.getByPlaceholder(/Ask AegisOps/);
  await box.fill(text);
  await box.press("Enter");
  await expect(page.getByText(text).last()).toBeVisible();
  // Wait for THIS turn to finish: the feedback row count equals the number of completed
  // assistant turns. (A `.last()` visibility check is satisfied by previous turns and lets
  // the next send fire mid-stream, where the composer drops it.)
  await expect(page.getByText("Was this helpful?")).toHaveCount(turnNo, { timeout: 120000 });
}

test.describe("continuous conversation with real memory (N-03)", () => {
  // The sidebar (with "New conversation") is a drawer on mobile; these journeys assert the
  // memory feature, which is viewport-independent — desktop coverage suffices.
  test.skip(({ isMobile }) => !!isMobile, "memory journeys run on desktop; mobile covered by core-flow");

  test("three turns then a recall turn lists the prior questions", async ({ page }) => {
    test.setTimeout(240000);
    await openWorkspace(page);
    // Start a FRESH thread so recall is deterministic.
    await page.getByRole("button", { name: "New conversation" }).click();

    const q1 = "What does IMDSv2 protect against on EC2?";
    const q2 = "How many VMs are running in aws right now?";
    const q3 = "What is a VPC CIDR block?";
    let turn = 0;
    for (const q of [q1, q2, q3]) {
      await send(page, q, ++turn);
    }

    await send(page, "What have I asked you so far in this conversation?", ++turn);

    // The recall answer must reference all three prior topics — never
    // "this is the beginning of our conversation" (screenshot 16) or
    // "my context window is blank" (screenshot 18).
    const thread = page.getByRole("main").or(page.locator("body"));
    await expect(thread.getByText(/beginning of our conversation/i)).toHaveCount(0);
    await expect(thread.getByText(/context window is (currently )?blank/i)).toHaveCount(0);
    for (const frag of [/IMDSv2/i, /VMs? .*running|running .*VMs?/i, /CIDR/i]) {
      await expect.poll(async () => (await page.locator("body").textContent()) ?? "", {
        timeout: 15000,
      }).toMatch(frag);
    }
  });

  test("'what was my previous question' resolves to the real prior turn", async ({ page }) => {
    test.setTimeout(180000);
    await openWorkspace(page);
    await page.getByRole("button", { name: "New conversation" }).click();

    await send(page, "Explain the difference between an NSG and a security group.", 1);
    await send(page, "What is my previous question to you ?", 2);

    await expect(page.getByText(/beginning of our conversation/i)).toHaveCount(0);
    await expect.poll(async () => (await page.locator("body").textContent()) ?? "", {
      timeout: 15000,
    }).toMatch(/NSG|security group/i);
  });
});
