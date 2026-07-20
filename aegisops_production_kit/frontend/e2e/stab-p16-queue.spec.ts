import { expect, test } from "@playwright/test";

// STAB P1-6 — a message typed while a turn streams queues VISIBLY and auto-sends.
// LIVE spec (two real LLM turns) — run on demand, excluded from routine canaries.
test("a fast follow-up queues visibly and lands as its own turn", async ({ page }) => {
  test.setTimeout(300000);
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "New conversation" }).click();

  const box = page.getByPlaceholder(/Ask AegisOps/);
  const q1 = `What does IMDSv2 protect against? p16-${Date.now() % 100000}`;
  await box.fill(q1);
  await box.press("Enter");
  await expect(box).toHaveValue("");            // first send accepted

  // Immediately send the follow-up while turn 1 streams.
  const q2 = "And what is a security group?";
  await box.fill(q2);
  await box.press("Enter");
  await expect(page.getByText(/Queued — sends when the current turn finishes/)).toBeVisible({ timeout: 5000 });

  // The queued turn auto-sends: its user message appears and gets its own answer.
  await expect(page.getByText(q2).last()).toBeVisible({ timeout: 240000 });
  await expect(page.getByText(/Queued — sends/)).toHaveCount(0, { timeout: 240000 });
  const answers = page.getByText(/Was this helpful\?/);
  await expect(answers.nth(1)).toBeVisible({ timeout: 240000 });   // two completed turns
});
