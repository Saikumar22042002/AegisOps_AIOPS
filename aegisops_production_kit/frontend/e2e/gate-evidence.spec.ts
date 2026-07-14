// Phase-2 exit-gate evidence (run on demand against the live stack):
//   npx playwright test e2e/gate-evidence.spec.ts --project=chromium
//
// 1. turn-20-of-100 recall IN THE UI — a real 100-turn session (seeded through the real
//    POST /chat endpoint) answers "What did I say in turn 20?" with the verbatim turn.
// 2. Real Traces tab — the run_steps-derived span tree with real durations (O1).
// 3. Honest model menu — only the models the backend actually serves (U3).
//
// Screenshots land in e2e/gate-out/.

import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

// Desktop-only by design (see header: run with --project=chromium): the flows drive the
// session sidebar, which is off-canvas on the mobile viewport. Mobile UX is covered by core-flow.
test.skip(({ isMobile }) => !!isMobile, "gate-evidence flows drive the desktop sidebar; mobile covered by core-flow");

test("turn-20-of-100 recall in the UI", async ({ page }) => {
  test.setTimeout(180000);
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });

  // Open the seeded 100-turn session from the sidebar (titled from its first message).
  await page.getByText("Ops log entry 1", { exact: false }).first().click();
  await expect(page.getByText("Ops log entry 100", { exact: false }).last())
    .toBeVisible({ timeout: 30000 });

  // Ask for turn 20 — the ANSWER must quote the verbatim user message from that turn. Assert on
  // the answer's unique prefix ("Your 20th user in this conversation was"), which exists only in
  // the assistant's recall reply — never in the seeded transcript itself (the phoenix sentence
  // alone would false-positive on the seeded turn-20 message).
  const composer = page.getByPlaceholder(/Ask AegisOps/);
  await composer.fill("What did I say in turn 20?");
  await composer.press("Enter");
  const answer = page.getByText(/Your 20th user in this conversation was/);
  await expect(answer).toBeVisible({ timeout: 120000 });
  await answer.scrollIntoViewIfNeeded();
  // The verbatim quote follows in the same reply.
  await expect(page.getByText(/phoenix cluster runs 17 nodes in Mumbai and its maintenance window/).last())
    .toBeVisible({ timeout: 15000 });
  await page.screenshot({ path: "e2e/gate-out/recall-turn-20.png", fullPage: false });
});

test("real Traces tab on the recall run", async ({ page }) => {
  test.setTimeout(120000);
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  await page.getByText("Ops log entry 1", { exact: false }).first().click();
  await expect(page.getByText(/Your 20th user in this conversation was/).last())
    .toBeVisible({ timeout: 30000 });

  // Open the last run's artifacts and switch to the Traces tab: the run_steps-derived tree
  // (root span + ordered children with REAL durations), never fabricated spans.
  await page.getByText("Traces", { exact: true }).first().click();
  await expect(page.getByText("Langfuse trace", { exact: false }).first())
    .toBeVisible({ timeout: 20000 });
  await expect(page.getByText("general run", { exact: false }).first())
    .toBeVisible({ timeout: 20000 });
  await expect(page.getByText(/router/).first()).toBeVisible({ timeout: 20000 });
  await expect(page.getByText("Open in Langfuse")).toBeVisible();
  await page.screenshot({ path: "e2e/gate-out/traces-tab.png", fullPage: false });
});

test("honest model menu — only served models", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });

  // Open the model selector: the menu lists exactly the backend-served Gemini ids (U3) —
  // no Claude/GPT/Azure/Llama entries we cannot run.
  await page.getByText("Model · LLM provider").isVisible().catch(() => {});
  const menuButton = page.locator("button", { hasText: /gemini|Gemini 2\.5 Pro/ }).first();
  await menuButton.click();
  await expect(page.getByText("Model · LLM provider")).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("gemini-3.5-flash", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("gemini-flash-latest", { exact: true })).toBeVisible();
  await expect(page.getByText("gemini-2.5-flash", { exact: true })).toBeVisible();
  await expect(page.getByText("GPT-4o")).toHaveCount(0);
  await expect(page.getByText("Claude Sonnet 4.5")).toHaveCount(0);
  await expect(page.getByText("Azure OpenAI")).toHaveCount(0);
  await expect(page.getByText("Llama 3.1 70B")).toHaveCount(0);
  await page.screenshot({ path: "e2e/gate-out/model-menu.png", fullPage: false });
});
