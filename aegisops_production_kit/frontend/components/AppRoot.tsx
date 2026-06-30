"use client";

import { useEffect } from "react";

import { useAuth } from "../lib/auth";
import { useUI } from "../lib/store";
import { AppShell } from "./AppShell";
import { LoginScreen } from "./LoginScreen";

export function AppRoot() {
  const { user, loading } = useAuth();
  const setSelector = useUI((s) => s.setSelector);

  // Reflect the authenticated user's primary role in the RBAC selector (the UI mirror;
  // the backend enforces the real role on every call regardless of this selection).
  useEffect(() => {
    if (user?.display_roles?.length) {
      setSelector("role", user.display_roles[0]);
    }
  }, [user, setSelector]);

  return (
    <div
      style={{
        height: "100vh",
        width: "100%",
        background: "var(--bg)",
        color: "var(--text-2)",
        fontSize: 14,
        overflow: "hidden",
        position: "relative",
      }}
    >
      {loading ? null : user ? <AppShell /> : <LoginScreen />}
    </div>
  );
}
