import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginScreen } from "../components/LoginScreen";
import { AuthProvider } from "../lib/auth";

describe("LoginScreen", () => {
  beforeEach(() => {
    // No backend in unit tests: /auth/me rejects → unauthenticated → login renders.
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
  });

  it("renders the sign-in card with both auth paths", () => {
    render(
      <AuthProvider>
        <LoginScreen />
      </AuthProvider>,
    );
    expect(screen.getByText("Sign in to AegisOps")).toBeInTheDocument();
    expect(screen.getByText("Continue with Keycloak SSO")).toBeInTheDocument();
    expect(screen.getByText("SAML · MFA enforced · SOC 2 Type II")).toBeInTheDocument();
  });
});
