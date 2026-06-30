"use client";

import { NavIcon } from "../lib/icons";
import { useUI } from "../lib/store";
import type { ArtifactTab, NavKey } from "../lib/types";

export function CommandPalette() {
  const cmdkOpen = useUI((s) => s.cmdkOpen);
  const closeCmdk = useUI((s) => s.closeCmdk);
  const cmdkQuery = useUI((s) => s.cmdkQuery);
  const setCmdkQuery = useUI((s) => s.setCmdkQuery);
  const newChat = useUI((s) => s.newChat);
  const openArtifact = useUI((s) => s.openArtifact);
  const approveRun = useUI((s) => s.approveRun);
  const navTo = useUI((s) => s.navTo);

  if (!cmdkOpen) return null;

  const openArt = (tab: ArtifactTab) => () => {
    openArtifact(tab);
    closeCmdk();
  };
  const go = (nav: NavKey) => () => navTo(nav);

  const actions = [
    { label: "New conversation", hint: "⌘N", d: "M12 5v14M5 12h14", color: "var(--accent-3)", run: () => { newChat(); closeCmdk(); } },
    { label: "Open Terraform plan", hint: "artifact", d: "m3 8 9-5 9 5-9 5-9-5Z", color: "var(--accent-3)", run: openArt("terraform") },
    { label: "View workflow timeline", hint: "artifact", d: "M6 6 18 9M9 18 8 8", color: "var(--accent-3)", run: openArt("timeline") },
    { label: "Approve & apply plan", hint: "action", d: "m5 12 5 5 9-11", color: "var(--green)", run: () => { void approveRun("approved"); closeCmdk(); } },
  ];
  const nav = [
    { label: "AI Workspace", d: "m12 3 1.9 4.6L18.5 9l-3.4 3 .9 4.8L12 14.6 7.9 16.8l.9-4.8L5.5 9l4.6-1.4L12 3Z", run: go("workspace") },
    { label: "Projects", d: "M3.5 9h17M8 5V3.5M16 5V3.5", run: go("projects") },
    { label: "Infrastructure", d: "M3 12h4l2.5-6 5 13 2.5-7H21", run: go("infrastructure") },
    { label: "Analytics", d: "M4 20V4M8 18v-6M14 18V9M20 18V6", run: go("analytics") },
  ];

  return (
    <div
      onClick={closeCmdk}
      style={{ position: "absolute", inset: 0, zIndex: 50, background: "rgba(5,5,7,.6)", backdropFilter: "blur(3px)", display: "flex", alignItems: "flex-start", justifyContent: "center", paddingTop: "13vh", animation: "ao-fadein .15s ease" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ width: 560, maxWidth: "90%", background: "var(--bg-pop)", border: "1px solid var(--border-2)", borderRadius: 15, boxShadow: "0 24px 70px rgba(0,0,0,.6)", overflow: "hidden", animation: "ao-fadeup .18s ease" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 11, padding: "16px 18px", borderBottom: "1px solid var(--border)" }}>
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
            <circle cx="11" cy="11" r="7" stroke="#6b6f7a" strokeWidth="2" />
            <path d="m20 20-3-3" stroke="#6b6f7a" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <input
            autoFocus
            value={cmdkQuery}
            onChange={(e) => setCmdkQuery(e.target.value)}
            placeholder="Search resources, run actions, jump to a page…"
            style={{ flex: 1, background: "transparent", border: "none", outline: "none", color: "var(--text)", fontSize: 15 }}
          />
          <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 10, color: "var(--text-4)", border: "1px solid var(--border-2)", borderRadius: 5, padding: "2px 6px" }}>ESC</span>
        </div>
        <div style={{ maxHeight: 340, overflowY: "auto", padding: 8 }}>
          <div style={cmdkEyebrow}>Actions</div>
          {actions.map((c) => (
            <button key={c.label} onClick={c.run} className="ao-h-cmd" style={cmdkRow}>
              <span style={{ ...cmdkIcon, color: "var(--accent-3)" }}>
                <NavIcon d={c.d} color={c.color} />
              </span>
              <span style={{ flex: 1, fontSize: 13.5, color: "var(--text)" }}>{c.label}</span>
              <span style={{ fontSize: 11, color: "var(--text-4)" }}>{c.hint}</span>
            </button>
          ))}
          <div style={cmdkEyebrow}>Navigate</div>
          {nav.map((c) => (
            <button key={c.label} onClick={c.run} className="ao-h-cmd" style={cmdkRow}>
              <span style={{ ...cmdkIcon, color: "var(--text-3)" }}>
                <NavIcon d={c.d} color="var(--text-3)" />
              </span>
              <span style={{ flex: 1, fontSize: 13.5, color: "var(--text)" }}>{c.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

const cmdkEyebrow: React.CSSProperties = {
  fontSize: 10,
  textTransform: "uppercase",
  letterSpacing: ".08em",
  color: "var(--text-5)",
  fontWeight: 600,
  padding: "8px 10px 6px",
};
const cmdkRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  width: "100%",
  padding: "10px 11px",
  borderRadius: 9,
  border: "none",
  background: "transparent",
  cursor: "pointer",
  textAlign: "left",
};
const cmdkIcon: React.CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: 8,
  background: "var(--surface-3)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
};
