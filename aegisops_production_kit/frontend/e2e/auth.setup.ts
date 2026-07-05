import { expect, test as setup } from "@playwright/test";

// Establishes an authenticated session ONCE (real Keycloak password grant with the seeded
// approver) and persists the session cookie. The browser projects reuse it via storageState,
// so authenticated tests don't each perform a fresh grant (which would trip the login rate limiter).
const authFile = "e2e/.auth/user.json";

setup("authenticate", async ({ page }) => {
  await page.goto("/");
  const signIn = page.getByRole("button", { name: "Sign in" });
  await expect(signIn).toBeVisible({ timeout: 15000 });
  await signIn.click();  // form is pre-filled with maya.okafor@northwind.com / aegisops
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });
  await page.context().storageState({ path: authFile });
});
