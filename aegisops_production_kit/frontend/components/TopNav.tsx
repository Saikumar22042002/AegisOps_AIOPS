"use client";

import { type CSSProperties, useEffect, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { cloudColor, modelColor } from "../lib/colors";
import { cloudOptions, roleOptions, type Opt } from "../lib/data";
import { Checkmark, ThemeGlyph } from "../lib/icons";
import { useResolvedTheme, useUI } from "../lib/store";

const popover: CSSProperties = {
  position: "absolute",
  top: "calc(100% + 8px)",
  background: "var(--bg-pop)",
  border: "1px solid var(--border-2)",
  borderRadius: 12,
  boxShadow: "0 16px 44px rgba(0,0,0,.4)",
  padding: 6,
  zIndex: 40,
  animation: "ao-fadeup .15s ease",
};

const menuEyebrow: CSSProperties = {
  fontSize: 10,
  textTransform: "uppercase",
  letterSpacing: ".08em",
  color: "var(--text-5)",
  fontWeight: 600,
  padding: "7px 10px 5px",
};

export function TopNav() {
  const menu = useUI((s) => s.menu);
  const closeMenus = useUI((s) => s.closeMenus);
  const toggleMobileNav = useUI((s) => s.toggleMobileNav);
  const anyMenu = menu !== null;

  return (
    <header
      id="ao-topnav"
      style={{
        height: 58,
        flexShrink: 0,
        borderBottom: "1px solid var(--surface-3)",
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "0 20px",
        background: "var(--bg)",
      }}
    >
      {anyMenu && <div onClick={closeMenus} style={{ position: "fixed", inset: 0, zIndex: 30 }} />}

      <button
        id="ao-hamburger"
        onClick={toggleMobileNav}
        style={{
          width: 34,
          height: 34,
          flexShrink: 0,
          borderRadius: 9,
          border: "1px solid var(--border-2)",
          background: "var(--surface-2)",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          color: "var(--text-2)",
        }}
      >
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
          <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      </button>

      <CloudSelector />

      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 9 }}>
        <ModelSelector />
        <ThemeMenu />
        <NotificationsMenu />
        <ArtifactToggle />
        <div style={{ width: 1, height: 22, background: "var(--border-2)" }} />
        <ProfileMenu />
      </div>
    </header>
  );
}

function CloudSelector() {
  const cloud = useUI((s) => s.cloud);
  const open = useUI((s) => s.menu === "cloud");
  const toggle = useUI((s) => s.toggleMenu);
  const setSelector = useUI((s) => s.setSelector);
  return (
    <div className="ao-cloud-sel" style={{ position: "relative", zIndex: 31 }}>
      <button
        onClick={() => toggle("cloud")}
        className="ao-h-b3"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "6px 10px",
          borderRadius: 8,
          border: "1px solid var(--border-2)",
          background: "var(--surface)",
          cursor: "pointer",
          fontSize: 12.5,
          color: "var(--text-2)",
        }}
      >
        <span style={{ width: 7, height: 7, borderRadius: 2, background: cloudColor(cloud) }} />
        {cloud}
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
          <path d="m7 10 5 5 5-5" stroke="var(--text-4)" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div style={{ ...popover, left: 0, minWidth: 220 }}>
          <div style={menuEyebrow}>Cloud accounts</div>
          {cloudOptions.map((o) => (
            <button
              key={o.label}
              onClick={() => setSelector("cloud", o.label)}
              className="ao-h-s2"
              style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 10px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left" }}
            >
              <span style={{ width: 8, height: 8, borderRadius: 2, background: o.dot, flexShrink: 0 }} />
              <span style={{ flex: 1, fontSize: 13, color: "var(--text)", fontWeight: 500 }}>{o.label}</span>
              <span style={{ fontSize: 11, color: "var(--text-4)" }}>{o.sub}</span>
              {cloud === o.label && <Checkmark />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ModelSelector() {
  const model = useUI((s) => s.model);
  const open = useUI((s) => s.menu === "model");
  const toggle = useUI((s) => s.toggleMenu);
  const setSelector = useUI((s) => s.setSelector);
  // P0/D4: the menu lists exactly what the backend serves — GET /models is the single
  // source of truth (replaces a hardcoded literal that required manual sync with the
  // backend registry). On failure the menu is honestly empty, never a fake list.
  const [modelOptions, setModelOptions] = useState<Opt[]>([]);
  useEffect(() => {
    api
      .get<{ models: { id: string; provider: string; enabled: boolean; default: boolean }[] }>("/models")
      .then((r) =>
        setModelOptions(
          r.models.map((m) => ({
            label: m.id,
            sub: `${m.provider}${m.default ? " · default" : ""}`,
            dot: modelColor(m.id),
          })),
        ),
      )
      .catch(() => setModelOptions([]));
  }, []);
  return (
    <div style={{ position: "relative", zIndex: 31 }}>
      <button
        onClick={() => toggle("model")}
        className="ao-h-b3"
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 11px", borderRadius: 9, border: "1px solid var(--border-2)", background: "var(--surface-2)", color: "var(--text-2)", fontSize: 12.5, cursor: "pointer" }}
      >
        <span style={{ width: 7, height: 7, borderRadius: 99, background: modelColor(model) }} />
        {model}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
          <path d="m7 10 5 5 5-5" stroke="var(--text-4)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div style={{ ...popover, right: 0, minWidth: 268 }}>
          <div style={menuEyebrow}>Model · LLM provider</div>
          {modelOptions.length === 0 && (
            <div style={{ padding: "9px 10px", fontSize: 12, color: "var(--text-4)" }}>
              model catalog unavailable (GET /models)
            </div>
          )}
          {modelOptions.map((o) => (
            <button
              key={o.label}
              onClick={() => setSelector("model", o.label)}
              className="ao-h-s2"
              style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 10px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left" }}
            >
              <span style={{ width: 8, height: 8, borderRadius: 99, background: o.dot, flexShrink: 0 }} />
              <span style={{ flex: 1 }}>
                <span style={{ display: "block", fontSize: 13, color: "var(--text)", fontWeight: 500 }}>{o.label}</span>
                <span style={{ display: "block", fontSize: 11, color: "var(--text-4)" }}>{o.sub}</span>
              </span>
              {model === o.label && <Checkmark />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ThemeMenu() {
  const theme = useUI((s) => s.theme);
  const resolved = useResolvedTheme();
  const open = useUI((s) => s.menu === "theme");
  const toggle = useUI((s) => s.toggleMenu);
  const setTheme = useUI((s) => s.setTheme);
  const row = (key: "dark" | "light" | "system", label: string) => (
    <button
      onClick={() => setTheme(key)}
      className="ao-h-s2"
      style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 10px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left", fontSize: 13, color: "var(--text)" }}
    >
      {label}
      {theme === key && <span style={{ marginLeft: "auto" }}><Checkmark /></span>}
    </button>
  );
  return (
    <div style={{ position: "relative", zIndex: 31 }}>
      <button
        onClick={() => toggle("theme")}
        className="ao-h-b3"
        style={{ width: 35, height: 35, borderRadius: 9, border: "1px solid var(--border-2)", background: "var(--surface-2)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", color: "var(--text-3)" }}
      >
        <ThemeGlyph t={resolved} />
      </button>
      {open && (
        <div style={{ ...popover, right: 0, minWidth: 170 }}>
          <div style={menuEyebrow}>Theme</div>
          {row("dark", "Dark")}
          {row("light", "Light")}
          {row("system", "System")}
        </div>
      )}
    </div>
  );
}

function NotificationsMenu() {
  const open = useUI((s) => s.menu === "notif");
  const toggle = useUI((s) => s.toggleMenu);
  const [items, setItems] = useState<{ title: string; time: string; color: string }[]>([]);
  useEffect(() => {
    if (open) {
      api.get<{ notifications: { title: string; time: string; color: string }[] }>("/notifications")
        .then((d) => setItems(d.notifications))
        .catch(() => setItems([]));
    }
  }, [open]);
  return (
    <div style={{ position: "relative", zIndex: 31 }}>
      <button
        onClick={() => toggle("notif")}
        className="ao-h-b3"
        style={{ width: 35, height: 35, borderRadius: 9, border: "1px solid var(--border-2)", background: "var(--surface-2)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", position: "relative" }}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z" stroke="var(--text-3)" strokeWidth="1.6" strokeLinejoin="round" />
          <path d="M10 19a2 2 0 0 0 4 0" stroke="var(--text-3)" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span style={{ position: "absolute", top: 7, right: 8, minWidth: 7, height: 7, borderRadius: 99, background: "var(--red)", border: "1.5px solid var(--bg-elev)" }} />
      </button>
      {open && (
        <div style={{ ...popover, right: 0, width: 320 }}>
          <div style={{ display: "flex", alignItems: "center", padding: "8px 10px 6px" }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text)" }}>Notifications</span>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--accent-3)", cursor: "pointer" }}>Mark all read</span>
          </div>
          {items.length === 0 && <div style={{ padding: "10px", fontSize: 12, color: "var(--text-4)" }}>No notifications</div>}
          {items.map((n) => (
            <button
              key={n.title}
              className="ao-h-s2"
              style={{ display: "flex", gap: 10, width: "100%", padding: "9px 10px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left" }}
            >
              <span style={{ width: 7, height: 7, borderRadius: 99, background: n.color, marginTop: 5, flexShrink: 0 }} />
              <span style={{ flex: 1, fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.45 }}>{n.title}</span>
              <span style={{ fontSize: 11, color: "var(--text-4)", flexShrink: 0 }}>{n.time}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ArtifactToggle() {
  const artifactOpen = useUI((s) => s.artifactOpen);
  const toggleArtifact = useUI((s) => s.toggleArtifact);
  return (
    <button
      onClick={toggleArtifact}
      className="ao-h-b3"
      style={{
        width: 35,
        height: 35,
        borderRadius: 9,
        border: "1px solid var(--border-2)",
        background: artifactOpen ? "rgba(99,102,241,.14)" : "var(--surface-2)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        cursor: "pointer",
        color: artifactOpen ? "var(--accent-3)" : "var(--text-3)",
      }}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
        <path d="M14 4.5v15" stroke="currentColor" strokeWidth="1.6" />
      </svg>
    </button>
  );
}

function ProfileMenu() {
  const open = useUI((s) => s.menu === "profile");
  const toggle = useUI((s) => s.toggleMenu);
  const role = useUI((s) => s.role);
  const setSelector = useUI((s) => s.setSelector);
  const navTo = useUI((s) => s.navTo);
  const { user, logout } = useAuth();
  const initials = (user?.name ?? "MO")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <div style={{ position: "relative", zIndex: 31 }}>
      <button
        onClick={() => toggle("profile")}
        className="ao-h-b3"
        style={{ display: "flex", alignItems: "center", gap: 8, padding: 3, borderRadius: 99, border: "1px solid var(--border-2)", background: "var(--surface-2)", cursor: "pointer" }}
      >
        <span style={{ width: 29, height: 29, borderRadius: 99, background: "var(--av-user-bg)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 600, color: "var(--av-user-fg)" }}>
          {initials}
        </span>
      </button>
      {open && (
        <div style={{ ...popover, right: 0, width: 288 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 11, padding: "10px 11px 12px" }}>
            <span style={{ width: 38, height: 38, borderRadius: 10, background: "var(--av-user-bg)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, fontWeight: 600, color: "var(--av-user-fg)" }}>
              {initials}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>{user?.name ?? "—"}</div>
              <div style={{ fontSize: 11.5, color: "var(--text-4)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user?.email ?? ""}</div>
            </div>
          </div>
          <div style={{ padding: "0 11px 8px" }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 500, color: "var(--accent-3)", padding: "3px 9px", borderRadius: 99, background: "rgba(99,102,241,.1)", border: "1px solid var(--accent-border)" }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
                <path d="M12 2.6 4.6 5.6v5.2c0 4.4 3 8.5 7.4 9.9 4.4-1.4 7.4-5.5 7.4-9.9V5.6L12 2.6Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
              </svg>
              {role}
            </span>
          </div>
          <div style={{ height: 1, background: "var(--border)", margin: "2px 0 6px" }} />
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--text-5)", fontWeight: 600, padding: "5px 11px 5px" }}>
            Switch role · RBAC
          </div>
          <div style={{ maxHeight: 184, overflowY: "auto" }}>
            {roleOptions.map((o) => (
              <button
                key={o.label}
                onClick={() => setSelector("role", o.label)}
                className="ao-h-s2"
                style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "8px 11px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left" }}
              >
                <span style={{ flex: 1 }}>
                  <span style={{ display: "block", fontSize: 12.5, color: "var(--text)", fontWeight: 500 }}>{o.label}</span>
                  <span style={{ display: "block", fontSize: 11, color: "var(--text-4)" }}>{o.sub}</span>
                </span>
                {role === o.label && <Checkmark />}
              </button>
            ))}
          </div>
          <div style={{ height: 1, background: "var(--border)", margin: "6px 0" }} />
          <button
            onClick={() => navTo("settings")}
            className="ao-h-s2"
            style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 11px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left", fontSize: 13, color: "var(--text-2)" }}
          >
            Preferences &amp; API keys
          </button>
          <button
            onClick={() => void logout()}
            className="ao-h-signout"
            style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 11px", borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", textAlign: "left", fontSize: 13, color: "var(--red-2)" }}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
