import { expect, test } from "@playwright/test";

// M1/M2 smoke: unauthenticated users land on the pixel-exact login screen. The full
// login → chat → approval → modules journeys are added across M2–M6 as features land.
test("login screen renders", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Sign in to AegisOps")).toBeVisible();
  await expect(page.getByText("Continue with Keycloak SSO")).toBeVisible();
});
