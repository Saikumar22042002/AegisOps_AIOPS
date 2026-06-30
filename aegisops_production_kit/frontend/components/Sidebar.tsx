"use client";

import { useState } from "react";

import { useAuth } from "../lib/auth";
import { BrandShield } from "../lib/icons";
import { useUI } from "../lib/store";
import { navStyle } from "../lib/styles";
import type { NavKey, SessionMeta } from "../lib/types";

const eyebrow = (text: string, extraPad?: string): React.CSSProperties => ({
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: ".09em",
  textTransform: "uppercase",
  color: "var(--text-5)",
  padding: extraPad ?? "6px 9px 6px",
});

function NavButton({
  navKey,
  label,
  d,
  rects,
  trailing,
}: {
  navKey: NavKey;
  label: string;
  d?: string;
  rects?: React.ReactNode;
  trailing?: React.ReactNode;
}) {
  const active = useUI((s) => s.activeNav === navKey);
  const navTo = useUI((s) => s.navTo);
  return (
    <button className={active ? undefined : "ao-h-s2"} style={navStyle(active)} onClick={() => navTo(navKey)}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        {rects}
        {d && <path d={d} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />}
      </svg>
      {label}
      {trailing}
    </button>
  );
}

export function Sidebar() {
  const mobileNavOpen = useUI((s) => s.mobileNavOpen);
  const newChat = useUI((s) => s.newChat);
  const openCmdk = useUI((s) => s.openCmdk);
  const overview = useUI((s) => s.overview);
  const { user } = useAuth();

  // Org identity comes from the authenticated org (real), falling back to the UI selector.
  const orgName = overview?.org?.name ?? useUI.getState().org;
  const orgInitials = orgName
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const roleShort = user?.display_roles?.[0]?.split(" ")[0] ?? "";
  const projectsCount = overview?.projects;
  const incidentsCount = overview?.incidents ?? 0;

  return (
    <aside
      id="ao-sidebar"
      data-open={mobileNavOpen}
      style={{
        width: 252,
        flexShrink: 0,
        background: "var(--bg-elev)",
        borderRight: "1px solid var(--surface-3)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
      }}
    >
      <div style={{ padding: "20px 18px 16px", display: "flex", alignItems: "center", gap: 11 }}>
        <div
          style={{
            width: 31,
            height: 31,
            borderRadius: 9,
            background: "linear-gradient(155deg,var(--accent),var(--accent-strong))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 3px 12px rgba(79,70,229,.4)",
          }}
        >
          <BrandShield size={16} />
        </div>
        <div style={{ lineHeight: 1.1 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text)", letterSpacing: "-.01em" }}>AegisOps</div>
          <div style={{ fontSize: 10.5, color: "var(--text-4)", fontWeight: 500 }}>AI Operations OS</div>
        </div>
      </div>

      <div style={{ padding: "2px 14px 14px" }}>
        <button
          onClick={newChat}
          className="ao-h-newchat"
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "11px 13px",
            borderRadius: 10,
            border: "1px solid rgba(129,140,248,.25)",
            background: "rgba(99,102,241,.1)",
            color: "var(--accent-fg)",
            fontSize: 13,
            fontWeight: 500,
            cursor: "pointer",
            transition: "all .15s",
          }}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
            <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          New conversation
        </button>
      </div>

      <div style={{ padding: "0 14px 14px" }}>
        <button
          onClick={openCmdk}
          className="ao-h-b3"
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "9px 12px",
            borderRadius: 9,
            background: "var(--surface-2)",
            border: "1px solid var(--surface-3)",
            cursor: "pointer",
            transition: "border-color .15s",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
            <circle cx="11" cy="11" r="7" stroke="#6b6f7a" strokeWidth="2" />
            <path d="m20 20-3-3" stroke="#6b6f7a" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <span style={{ color: "var(--text-4)", fontSize: 12.5 }}>Search &amp; commands</span>
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "'IBM Plex Mono',monospace",
              fontSize: 10,
              color: "var(--text-3)",
              border: "1px solid var(--border-2)",
              borderRadius: 5,
              padding: "1px 6px",
            }}
          >
            ⌘K
          </span>
        </button>
      </div>

      <nav style={{ padding: "4px 10px 4px", display: "flex", flexDirection: "column", gap: 2 }}>
        <div style={eyebrow("Platform")}>Platform</div>
        <NavButton navKey="workspace" label="AI Workspace" d="m12 3 1.9 4.6L18.5 9l-3.4 3 .9 4.8L12 14.6 7.9 16.8l.9-4.8L5.5 9l4.6-1.4L12 3Z" />
        <NavButton
          navKey="projects"
          label="Projects"
          d="M3.5 9h17M8 5V3.5M16 5V3.5"
          rects={<rect x="3.5" y="5" width="17" height="14" rx="2.5" stroke="currentColor" strokeWidth="1.6" />}
          trailing={
            projectsCount != null ? (
              <span style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--text-5)", fontFamily: "'IBM Plex Mono',monospace" }}>{projectsCount}</span>
            ) : undefined
          }
        />
        <NavButton
          navKey="infrastructure"
          label="Infrastructure"
          rects={
            <>
              <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
              <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
              <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
              <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
            </>
          }
        />
        <NavButton
          navKey="incidents"
          label="Incidents"
          d="M12 3 2.5 20h19L12 3ZM12 10v4M12 17h.01"
          trailing={
            incidentsCount > 0 ? (
              <span
                style={{
                  marginLeft: "auto",
                  fontSize: 10,
                  fontWeight: 600,
                  color: "var(--amber)",
                  padding: "1px 7px",
                  borderRadius: 99,
                  background: "rgba(251,191,36,.12)",
                }}
              >
                {incidentsCount}
              </span>
            ) : undefined
          }
        />
        <NavButton navKey="knowledge" label="Knowledge" d="M5 4.5h9a2.5 2.5 0 0 1 2.5 2.5v12.5H7.5A2.5 2.5 0 0 1 5 19V4.5ZM9 9h4M9 12.5h4" />
        <NavButton
          navKey="analytics"
          label="Analytics"
          d="M4 20V4"
          rects={
            <>
              <rect x="7.5" y="12" width="3" height="6" rx="1" stroke="currentColor" strokeWidth="1.5" />
              <rect x="13" y="8" width="3" height="10" rx="1" stroke="currentColor" strokeWidth="1.5" />
              <rect x="18.5" y="5" width="3" height="13" rx="1" stroke="currentColor" strokeWidth="1.5" />
            </>
          }
        />
        <NavButton
          navKey="admin"
          label="Administration"
          d="M12 2.5v2.2M12 19.3v2.2M21.5 12h-2.2M4.7 12H2.5M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6M18.4 18.4l-1.6-1.6M7.2 7.2 5.6 5.6"
          rects={<circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" />}
        />
      </nav>

      <div style={{ flex: 1, overflowY: "auto", padding: "16px 10px 8px", minHeight: 0 }}>
        <SessionHistory />
      </div>

      <div style={{ borderTop: "1px solid var(--surface-3)", padding: "11px 12px" }}>
        <SettingsNav />
        <button
          className="ao-h-s2"
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: 8,
            marginTop: 4,
            borderRadius: 10,
            border: "none",
            background: "transparent",
            cursor: "pointer",
          }}
        >
          <div
            style={{
              width: 30,
              height: 30,
              borderRadius: 8,
              background: "var(--av-org-bg)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 12,
              fontWeight: 600,
              color: "var(--av-org-fg)",
              flexShrink: 0,
            }}
          >
            {orgInitials}
          </div>
          <div style={{ textAlign: "left", lineHeight: 1.2, flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 12.5, fontWeight: 500, color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {orgName}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-4)" }}>
              {user?.name ?? "—"}
              {roleShort ? ` · ${roleShort}` : ""}
            </div>
          </div>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
            <path d="m8 9 4-4 4 4M8 15l4 4 4-4" stroke="#6b6f7a" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </aside>
  );
}

function dayBucket(iso: string): "Today" | "Yesterday" | "Earlier" {
  const d = new Date(iso);
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayMs = 86400000;
  if (t >= startToday) return "Today";
  if (t >= startToday - dayMs) return "Yesterday";
  return "Earlier";
}

function SessionHistory() {
  // Real persisted conversations (org-scoped, newest-first) — replaces the hardcoded list.
  const sessions = useUI((s) => s.sessions);

  if (!sessions.length) {
    return (
      <div style={{ padding: "10px 11px", color: "var(--text-5)", fontSize: 12, lineHeight: 1.6 }}>
        <div style={{ fontWeight: 500, color: "var(--text-4)" }}>No conversations yet</div>
        <div style={{ marginTop: 3 }}>Start one with “New conversation”.</div>
      </div>
    );
  }

  const order: ("Today" | "Yesterday" | "Earlier")[] = ["Today", "Yesterday", "Earlier"];
  const groups: Record<string, SessionMeta[]> = { Today: [], Yesterday: [], Earlier: [] };
  for (const s of sessions) groups[dayBucket(s.created_at)].push(s);

  return (
    <>
      {order.map((label, gi) =>
        groups[label].length === 0 ? null : (
          <div key={label}>
            <div style={eyebrow(label, gi === 0 ? "2px 9px 9px" : "16px 9px 9px")}>{label}</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {groups[label].map((s) => (
                <SessionRow key={s.id} s={s} />
              ))}
            </div>
          </div>
        ),
      )}
    </>
  );
}

function SessionRow({ s }: { s: SessionMeta }) {
  const active = useUI((st) => st.sessionId === s.id);
  const openSession = useUI((st) => st.openSession);
  const renameSession = useUI((st) => st.renameSession);
  const deleteSession = useUI((st) => st.deleteSession);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(s.title);
  const [confirming, setConfirming] = useState(false);

  const commitRename = () => {
    setEditing(false);
    const t = draft.trim();
    if (t && t !== s.title) void renameSession(s.id, t);
    else setDraft(s.title);
  };

  const iconBtn = (onClick: () => void, title: string, children: React.ReactNode) => (
    <span
      role="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      style={{ width: 22, height: 22, borderRadius: 6, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-4)", flexShrink: 0, cursor: "pointer" }}
    >
      {children}
    </span>
  );

  if (editing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 8px" }}>
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitRename();
            if (e.key === "Escape") {
              setEditing(false);
              setDraft(s.title);
            }
          }}
          onBlur={commitRename}
          style={{ flex: 1, minWidth: 0, background: "var(--surface-2)", border: "1px solid var(--accent-2)", borderRadius: 7, color: "var(--text)", fontSize: 12.5, padding: "5px 8px", outline: "none" }}
        />
      </div>
    );
  }

  return (
    <div
      className="ao-h-s2 ao-session-row"
      onClick={() => void openSession(s.id)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "9px 10px",
        borderRadius: 8,
        background: active ? "rgba(99,102,241,.1)" : "transparent",
        color: active ? "var(--text-navactive)" : "var(--text-3)",
        fontSize: 12.5,
        cursor: "pointer",
        boxShadow: active ? "inset 2px 0 0 var(--accent-2)" : "none",
      }}
    >
      <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.title}</span>
      {confirming ? (
        <span style={{ display: "flex", gap: 4, flexShrink: 0 }}>
          {iconBtn(() => void deleteSession(s.id), "Confirm delete",
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="m5 12 5 5 9-11" stroke="var(--red)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>)}
          {iconBtn(() => setConfirming(false), "Cancel",
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>)}
        </span>
      ) : (
        <span className="ao-session-actions" style={{ display: "flex", gap: 2, flexShrink: 0 }}>
          {iconBtn(() => { setDraft(s.title); setEditing(true); }, "Rename",
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>)}
          {iconBtn(() => setConfirming(true), "Delete",
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>)}
        </span>
      )}
    </div>
  );
}

function SettingsNav() {
  const active = useUI((s) => s.activeNav === "settings");
  const navTo = useUI((s) => s.navTo);
  return (
    <button className={active ? undefined : "ao-h-s2"} style={navStyle(active)} onClick={() => navTo("settings")}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
        <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" stroke="currentColor" strokeWidth="1.6" />
        <path
          d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L16 2H8l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 3 12a7 7 0 0 0 .1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L8 22h8l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinejoin="round"
          opacity=".55"
        />
      </svg>
      Settings
    </button>
  );
}
