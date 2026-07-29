// GW-1 canary — the "Link Telegram" control, live, at its real UI location:
//   left sidebar → Settings → "Connected accounts" → Telegram row.
//
//   npx playwright test e2e/gw1-telegram-link.spec.ts --project=chromium
//
// The spec asserts the posture the API actually reports, so it is honest in BOTH deployments and
// does not go red the moment an operator pastes a bot token:
//
//   enabled:false (no TELEGRAM_BOT_TOKEN)  → the row says so and "Generate code" is disabled;
//   enabled:true                           → "Generate code" issues a one-time code, the code is
//                                            shown with a live expiry countdown, and the /link
//                                            instruction names the bot.
//
// Screenshot lands in e2e/gate-out/.

import { expect, test } from "@playwright/test";

// Desktop-only: the Settings entry lives in the left sidebar, which is off-canvas on mobile.
test.skip(({ isMobile }) => !!isMobile, "Settings nav lives in the desktop sidebar");

test("Settings → Connected accounts exposes the Telegram link control", async ({ page }) => {
  test.setTimeout(90000);

  // Read the backend's own view of the gateway first — the UI must agree with it.
  await page.goto("/");
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  const status = await page.evaluate(async () => {
    const base = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
    const r = await fetch(`${base}/gateways/telegram`, { credentials: "include" });
    return r.ok ? await r.json() : { error: r.status };
  });
  expect(status.error, `GET /gateways/telegram failed: ${JSON.stringify(status)}`).toBeUndefined();
  expect(status.channel).toBe("telegram");

  // Navigate the way a user does: the left sidebar's Settings entry.
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByText("Profile & Settings")).toBeVisible({ timeout: 20000 });

  // The control's exact location.
  const panel = page.getByTestId("connected-accounts");
  await expect(panel).toBeVisible({ timeout: 20000 });
  await expect(panel.getByText("Connected accounts")).toBeVisible();
  await expect(panel.getByText(/Telegram/)).toBeVisible();

  // The Preferences list above it carries the same fact, so the two cannot disagree.
  await expect(page.getByText(/your roles and approval rules follow the link|not enabled on this deployment/))
    .toBeVisible();

  if (!status.enabled) {
    // Honest disabled posture: the control is present, explained, and inert.
    await expect(panel.getByText("disabled")).toBeVisible();
    await expect(panel.getByText(/Not enabled on this deployment/)).toBeVisible();
    await expect(panel.getByTestId("telegram-generate")).toBeDisabled();
    await expect(panel.getByTestId("telegram-code")).toHaveCount(0);
  } else if (status.linked) {
    await expect(panel.getByText("linked")).toBeVisible();
    await expect(panel.getByTestId("telegram-unlink")).toBeEnabled();
  } else {
    // Enabled and unlinked: issuing a code shows it once, with a countdown and the /link line.
    await expect(panel.getByText("not linked")).toBeVisible();
    const generate = panel.getByTestId("telegram-generate");
    await expect(generate).toBeEnabled();
    await generate.click();

    const code = panel.getByTestId("telegram-code");
    await expect(code).toBeVisible({ timeout: 20000 });
    // Grouped 8-char code from the unambiguous alphabet (no 0/O/1/I/L).
    await expect(code.getByText(/^[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}$/))
      .toBeVisible();
    await expect(code.getByText(/expires in \d+:\d{2}/)).toBeVisible();
    await expect(code.getByText(/\/link/)).toBeVisible();
    await expect(code.getByText(/Single-use/)).toBeVisible();
  }

  await page.screenshot({ path: "e2e/gate-out/gw1-telegram-link.png", fullPage: false });
});
