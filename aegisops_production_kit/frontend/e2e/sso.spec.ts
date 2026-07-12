import { expect, test } from "@playwright/test";

// Keycloak SSO round-trip (Authorization Code + PKCE) — regression guard for the 2026-07-06
// fix: the SSO button used to redirect the browser to http://keycloak:8080 (a docker service
// name the browser can't resolve), and the callback then rejected the token's issuer.
//
// Runs UNAUTHENTICATED (fresh context, no storageState) and performs exactly ONE interactive
// Keycloak login — a single extra grant per suite run, which stays well under the login
// rate limiter the password-grant setup already accounts for.
test.use({ storageState: { cookies: [], origins: [] } });

test("Continue with Keycloak SSO signs in and back out", async ({ page }) => {
  await page.goto("/");
  const ssoButton = page.getByRole("button", { name: /Continue with Keycloak SSO/ });
  await expect(ssoButton).toBeVisible({ timeout: 15000 });

  await ssoButton.click();

  // Real Keycloak login page, on a browser-reachable host (never the docker service name).
  await page.waitForURL(/\/realms\/aegisops\/protocol\/openid-connect\/auth/, { timeout: 15000 });
  expect(new URL(page.url()).hostname).not.toBe("keycloak");
  await page.locator("#username").fill("maya.okafor@northwind.com");
  await page.locator("#password").fill("aegisops");
  await page.locator("#kc-login").click();

  // Back in AegisOps, authenticated: the workspace composer renders.
  await expect(page.getByPlaceholder(/Ask AegisOps/)).toBeVisible({ timeout: 30000 });

  // The session is real: /auth/me answers 200 with the SSO user.
  const me = await page.request.get(
    (process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000") + "/auth/me",
  );
  expect(me.status()).toBe(200);
  expect((await me.json()).user.username).toBe("maya.okafor");
});
