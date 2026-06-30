"use client";

import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { useUI } from "../lib/store";

interface ModuleData {
  eyebrow: string; title: string; icon: string; desc: string; listTitle: string;
  stats: { label: string; value: string; delta: string; deltaColor: string }[];
  rows: { dot: string; name: string; meta: string; value: string }[];
}
interface IntegrationRow { name: string; cat: string; mark: string; color: string; status: string; statusColor: string }

export function ModuleView() {
  const activeNav = useUI((s) => s.activeNav);
  const navTo = useUI((s) => s.navTo);
  const [meta, setMeta] = useState<ModuleData | null>(null);
  const [integrations, setIntegrations] = useState<IntegrationRow[]>([]);

  useEffect(() => {
    if (activeNav === "workspace") return;
    let alive = true;
    setMeta(null);
    api.get<ModuleData>(`/modules/${activeNav}`).then((d) => alive && setMeta(d)).catch(() => alive && setMeta(null));
    if (activeNav === "admin") {
      api.get<{ integrations: IntegrationRow[] }>("/integrations").then((d) => alive && setIntegrations(d.integrations)).catch(() => {});
    }
    return () => { alive = false; };
  }, [activeNav]);

  const isAdmin = activeNav === "admin";

  return (
    <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
      <div className="ao-module-pad" style={{ maxWidth: 1080, margin: "0 auto", padding: "40px 36px" }}>
        {!meta ? (
          <div style={{ fontSize: 13, color: "var(--text-4)", padding: "20px 0" }}>Loading {activeNav}…</div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 17, marginBottom: 32 }}>
              <div style={{ width: 46, height: 46, borderRadius: 13, background: "rgba(99,102,241,.12)", border: "1px solid rgba(129,140,248,.25)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, color: "var(--accent-3)", fontWeight: 600 }}>{meta.icon}</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--text-4)", marginBottom: 5 }}>{meta.eyebrow}</div>
                <div style={{ fontSize: 24, fontWeight: 600, color: "var(--text)", letterSpacing: "-.02em" }}>{meta.title}</div>
                <div style={{ fontSize: 13.5, color: "var(--text-3)", marginTop: 6, maxWidth: 580, lineHeight: 1.65 }}>{meta.desc}</div>
              </div>
              <button onClick={() => navTo("workspace")} className="ao-h-modulecta" style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 15px", borderRadius: 10, border: "1px solid rgba(129,140,248,.3)", background: "rgba(99,102,241,.1)", color: "var(--accent-fg)", fontSize: 13, fontWeight: 500, cursor: "pointer", flexShrink: 0 }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="m12 3 1.9 4.6L18.5 9l-3.4 3 .9 4.8L12 14.6 7.9 16.8l.9-4.8L5.5 9l4.6-1.4L12 3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>Ask AI
              </button>
            </div>

            <div className="ao-stat-grid" style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14, marginBottom: 30 }}>
              {meta.stats.map((s) => (
                <div key={s.label} style={{ border: "1px solid var(--border)", borderRadius: 13, padding: "17px 18px", background: "var(--surface)" }}>
                  <div style={{ fontSize: 11, color: "var(--text-3)", marginBottom: 9 }}>{s.label}</div>
                  <div style={{ fontSize: 25, fontWeight: 600, color: "var(--text)", letterSpacing: "-.02em" }}>{s.value}</div>
                  <div style={{ fontSize: 11.5, color: s.deltaColor, marginTop: 6 }}>{s.delta}</div>
                </div>
              ))}
            </div>

            <div style={{ border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden", background: "var(--surface)" }}>
              <div style={{ padding: "15px 20px", borderBottom: "1px solid var(--surface-3)", display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>{meta.listTitle}</span>
                <span style={{ fontSize: 11, color: "var(--text-4)" }}>live data · org-scoped</span>
              </div>
              {meta.rows.map((r, i) => (
                <div key={i} className="ao-h-row" style={{ display: "flex", alignItems: "center", gap: 15, padding: "15px 20px", borderBottom: "1px solid var(--surface-2)", cursor: "pointer" }}>
                  <span style={{ width: 8, height: 8, borderRadius: 99, background: r.dot, flexShrink: 0 }} />
                  <span style={{ fontSize: 13.5, color: "var(--text)", fontWeight: 500, minWidth: 210 }}>{r.name}</span>
                  <span style={{ fontSize: 12.5, color: "var(--text-3)", flex: 1 }}>{r.meta}</span>
                  <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12, color: "var(--text-3)" }}>{r.value}</span>
                </div>
              ))}
            </div>

            {isAdmin && (
              <div style={{ marginTop: 26 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
                  <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Integrations &amp; connected services</span>
                  <span style={{ fontSize: 11, color: "var(--text-4)" }}>live health</span>
                </div>
                <div className="ao-summary-grid" style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12 }}>
                  {integrations.map((i) => (
                    <div key={i.name} className="ao-h-int" style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 15px", border: "1px solid var(--border)", borderRadius: 12, background: "var(--surface)" }}>
                      <span style={{ width: 34, height: 34, borderRadius: 9, background: "var(--surface-2)", border: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 700, color: i.color, flexShrink: 0 }}>{i.mark}</span>
                      <div style={{ flex: 1, minWidth: 0 }}><div style={{ fontSize: 13, color: "var(--text)", fontWeight: 500 }}>{i.name}</div><div style={{ fontSize: 11, color: "var(--text-4)" }}>{i.cat}</div></div>
                      <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10.5, color: i.statusColor }}><span style={{ width: 6, height: 6, borderRadius: 99, background: i.statusColor }} />{i.status}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
