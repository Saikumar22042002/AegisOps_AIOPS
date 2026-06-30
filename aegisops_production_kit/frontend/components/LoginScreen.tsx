"use client";

import { useState } from "react";

import { useAuth } from "../lib/auth";
import { BrandShield, ThemeGlyph } from "../lib/icons";
import { useResolvedTheme, useUI } from "../lib/store";

const THEME_LABEL: Record<string, string> = { dark: "Dark", light: "Light", system: "System" };

export function LoginScreen() {
  const { login, ssoLogin, error } = useAuth();
  const theme = useUI((s) => s.theme);
  const resolvedTheme = useResolvedTheme();
  const cycleTheme = useUI((s) => s.cycleTheme);

  const [email, setEmail] = useState("maya.okafor@northwind.com");
  const [password, setPassword] = useState("aegisops");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await login(email, password);
    } catch {
      /* error surfaced via auth context */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        height: "100%",
        width: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background:
          "radial-gradient(900px 500px at 50% -10%, rgba(99,102,241,.12), transparent 60%), var(--bg)",
        position: "relative",
      }}
    >
      <div style={{ position: "absolute", top: 26, left: 28, display: "flex", alignItems: "center", gap: 10 }}>
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: 8,
            background: "linear-gradient(155deg,var(--accent),var(--accent-strong))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <BrandShield size={15} />
        </div>
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>AegisOps</span>
      </div>
      <button
        onClick={cycleTheme}
        style={{
          position: "absolute",
          top: 24,
          right: 28,
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "7px 12px",
          borderRadius: 9,
          border: "1px solid var(--border-2)",
          background: "var(--surface-2)",
          color: "var(--text-3)",
          fontSize: 12,
          cursor: "pointer",
        }}
      >
        <ThemeGlyph t={resolvedTheme} /> {THEME_LABEL[theme]}
      </button>

      <div
        style={{
          width: 404,
          maxWidth: "92%",
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: 18,
          boxShadow: "0 30px 80px rgba(0,0,0,.4)",
          padding: "34px 34px 26px",
        }}
      >
        <div
          style={{
            width: 46,
            height: 46,
            borderRadius: 13,
            background: "linear-gradient(155deg,var(--accent),var(--accent-strong))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            marginBottom: 20,
            boxShadow: "0 6px 20px rgba(79,70,229,.4)",
          }}
        >
          <BrandShield size={24} />
        </div>
        <div style={{ fontSize: 21, fontWeight: 600, color: "var(--text)", letterSpacing: "-.02em" }}>
          Sign in to AegisOps
        </div>
        <div style={{ fontSize: 13.5, color: "var(--text-3)", marginTop: 6, marginBottom: 24 }}>
          AI-native CloudOps · DevOps · SRE platform
        </div>

        <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "var(--text-3)", marginBottom: 7 }}>
          Work email
        </label>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "11px 13px",
            borderRadius: 10,
            background: "var(--surface-2)",
            border: "1px solid var(--border-2)",
            marginBottom: 15,
          }}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="5" width="18" height="14" rx="2.5" stroke="var(--text-4)" strokeWidth="1.6" />
            <path d="m4 7 8 6 8-6" stroke="var(--text-4)" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
          <input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5 }}
          />
        </div>
        <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "var(--text-3)", marginBottom: 7 }}>
          Password
        </label>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "11px 13px",
            borderRadius: 10,
            background: "var(--surface-2)",
            border: "1px solid var(--border-2)",
            marginBottom: error ? 10 : 20,
          }}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
            <rect x="5" y="10" width="14" height="10" rx="2.5" stroke="var(--text-4)" strokeWidth="1.6" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" stroke="var(--text-4)" strokeWidth="1.6" />
          </svg>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
            style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, letterSpacing: ".15em" }}
          />
        </div>
        {error && (
          <div style={{ fontSize: 12, color: "var(--red-2)", marginBottom: 14 }} role="alert">
            {error}
          </div>
        )}
        <button
          onClick={() => void submit()}
          disabled={busy}
          style={{
            width: "100%",
            padding: 12,
            borderRadius: 10,
            border: "none",
            background: "var(--accent)",
            color: "#fff",
            fontSize: 14,
            fontWeight: 600,
            cursor: busy ? "default" : "pointer",
            opacity: busy ? 0.8 : 1,
            boxShadow: "0 4px 14px rgba(99,102,241,.3)",
          }}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "18px 0" }}>
          <span style={{ flex: 1, height: 1, background: "var(--border-2)" }} />
          <span style={{ fontSize: 11, color: "var(--text-4)" }}>OR</span>
          <span style={{ flex: 1, height: 1, background: "var(--border-2)" }} />
        </div>

        <button
          onClick={ssoLogin}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 10,
            padding: 11,
            borderRadius: 10,
            border: "1px solid var(--border-2)",
            background: "var(--surface-2)",
            color: "var(--text)",
            fontSize: 13.5,
            fontWeight: 500,
            cursor: "pointer",
          }}
        >
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
            <circle cx="9" cy="9" r="3.2" stroke="var(--accent-3)" strokeWidth="1.7" />
            <path d="m11 11 7.5 7.5M16 16l2.5-1 1 2.5M19 13.5 21 15" stroke="var(--accent-3)" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Continue with Keycloak SSO
        </button>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 7,
            marginTop: 18,
            fontSize: 11.5,
            color: "var(--text-4)",
          }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
            <path d="M12 2.6 4.6 5.6v5.2c0 4.4 3 8.5 7.4 9.9 4.4-1.4 7.4-5.5 7.4-9.9V5.6L12 2.6Z" stroke="var(--green)" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
          SAML · MFA enforced · SOC 2 Type II
        </div>
      </div>
    </div>
  );
}
