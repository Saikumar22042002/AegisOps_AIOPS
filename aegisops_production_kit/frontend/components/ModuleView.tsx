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

type MemoryRow = { key: string; content: string; scope: "user" | "org" };

// M4: user-editable standing memory ("usual_region: ap-south-1") — survives sessions and is
// threaded into every LLM call; "my usual region" also resolves deterministically.
function MemoryPanel() {
  const [rows, setRows] = useState<MemoryRow[]>([]);
  const [k, setK] = useState("");
  const [v, setV] = useState("");
  const [err, setErr] = useState("");

  const load = () =>
    api.get<{ memories: MemoryRow[] }>("/memory").then((d) => setRows(d.memories)).catch(() => {});
  useEffect(() => { load(); }, []);

  const save = async () => {
    setErr("");
    try {
      await api.put(`/memory/${encodeURIComponent(k.trim())}`, { content: v.trim(), org_wide: false });
      setK(""); setV(""); load();
    } catch (e: any) { setErr(String(e?.message ?? e)); }
  };
  const remove = async (key: string) => {
    setErr("");
    try { await api.del(`/memory/${encodeURIComponent(key)}`); load(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
  };

  return (
    <div style={{ marginTop: 26 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Standing memory</span>
        <span style={{ fontSize: 11, color: "var(--text-4)" }}>
          user-editable · survives sessions · e.g. key <code>usual_region</code> → “my usual region” is honored
        </span>
      </div>
      {err && <div style={{ fontSize: 12, color: "var(--red-2)", marginBottom: 10 }}>{err}</div>}
      <div style={{ border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden", background: "var(--surface)" }}>
        {rows.map((m) => (
          <div key={`${m.scope}:${m.key}`} style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 16px", borderBottom: "1px solid var(--surface-2)" }}>
            <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12.5, color: "var(--accent-3)", minWidth: 130 }}>{m.key}</span>
            <span style={{ fontSize: 12.5, color: "var(--text-2)", flex: 1 }}>{m.content}</span>
            <span style={{ fontSize: 10.5, color: "var(--text-4)" }}>{m.scope}</span>
            {m.scope === "user" && (
              <button onClick={() => remove(m.key)} className="ao-h-b3" title="Forget"
                style={{ padding: "3px 9px", borderRadius: 6, border: "1px solid var(--border-2)", background: "transparent", color: "var(--text-4)", fontSize: 11, cursor: "pointer" }}>
                forget
              </button>
            )}
          </div>
        ))}
        <div style={{ display: "flex", gap: 8, padding: "11px 16px" }}>
          <input value={k} onChange={(e) => setK(e.target.value)} placeholder="key (e.g. usual_region)"
            style={{ width: 200, padding: "7px 10px", borderRadius: 8, border: "1px solid var(--border-2)", background: "var(--surface-2)", color: "var(--text)", fontSize: 12.5, fontFamily: "'IBM Plex Mono',monospace" }} />
          <input value={v} onChange={(e) => setV(e.target.value)} placeholder="value (e.g. ap-south-1)"
            style={{ flex: 1, padding: "7px 10px", borderRadius: 8, border: "1px solid var(--border-2)", background: "var(--surface-2)", color: "var(--text)", fontSize: 12.5 }} />
          <button onClick={save} disabled={!k.trim() || !v.trim()} className="ao-h-b3"
            style={{ padding: "7px 14px", borderRadius: 8, border: "1px solid rgba(129,140,248,.3)", background: "rgba(99,102,241,.1)", color: "var(--accent-fg)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
            Remember
          </button>
        </div>
      </div>
    </div>
  );
}

type ProposalRow = {
  id: string; key: string; status: string; description?: string | null;
  fmt_ok?: boolean | null; validate_ok?: boolean | null; scan?: string | null;
  created_by?: string | null; reviewed_by?: string | null; created: string;
};

export function ModuleView() {
  const activeNav = useUI((s) => s.activeNav);
  const navTo = useUI((s) => s.navTo);
  const [meta, setMeta] = useState<ModuleData | null>(null);
  const [integrations, setIntegrations] = useState<IntegrationRow[]>([]);
  const [proposals, setProposals] = useState<ProposalRow[]>([]);
  const [reviewErr, setReviewErr] = useState<string>("");

  useEffect(() => {
    if (activeNav === "workspace") return;
    let alive = true;
    setMeta(null);
    api.get<ModuleData>(`/modules/${activeNav}`).then((d) => alive && setMeta(d)).catch(() => alive && setMeta(null));
    if (activeNav === "admin") {
      api.get<{ integrations: IntegrationRow[] }>("/integrations").then((d) => alive && setIntegrations(d.integrations)).catch(() => {});
    }
    if (activeNav === "infrastructure") {
      api.get<{ proposals: ProposalRow[] }>("/modules/proposals").then((d) => alive && setProposals(d.proposals)).catch(() => {});
    }
    return () => { alive = false; };
  }, [activeNav]);

  // MPP: the human review gate — promote (fail-closed on scan) or reject. RBAC errors from the
  // backend surface verbatim (approver roles only).
  const review = async (id: string, decision: "promote" | "reject") => {
    setReviewErr("");
    try {
      await api.post(`/modules/proposals/${id}/review`, { decision, note: "" });
      const d = await api.get<{ proposals: ProposalRow[] }>("/modules/proposals");
      setProposals(d.proposals);
    } catch (e: any) {
      setReviewErr(String(e?.message ?? e));
    }
  };

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

            {activeNav === "infrastructure" && proposals.length > 0 && (
              <div style={{ marginTop: 26 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
                  <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Module proposals</span>
                  <span style={{ fontSize: 11, color: "var(--text-4)" }}>
                    draft → checks → proposed → promoted · a drafted module is unselectable until promoted
                  </span>
                </div>
                {reviewErr && (
                  <div style={{ fontSize: 12, color: "var(--red-2)", marginBottom: 10 }}>{reviewErr}</div>
                )}
                <div style={{ border: "1px solid var(--border)", borderRadius: 14, overflow: "hidden", background: "var(--surface)" }}>
                  {proposals.map((p) => {
                    const statusColor = p.status === "promoted" ? "var(--green)"
                      : p.status === "rejected" ? "var(--red)"
                      : p.status === "proposed" ? "var(--amber)" : "var(--text-4)";
                    const check = (v?: boolean | null) => v == null ? "—" : v ? "✓" : "✗";
                    return (
                      <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 14, padding: "13px 18px", borderBottom: "1px solid var(--surface-2)" }}>
                        <span style={{ width: 8, height: 8, borderRadius: 99, background: statusColor, flexShrink: 0 }} />
                        <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 13, color: "var(--text)", minWidth: 130 }}>{p.key}</span>
                        <span style={{ fontSize: 12, color: "var(--text-3)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.description ?? ""}</span>
                        <span style={{ fontSize: 11, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-4)" }} title="fmt / validate / scan">
                          fmt {check(p.fmt_ok)} · validate {check(p.validate_ok)} · scan {p.scan ?? "not run"}
                        </span>
                        <span style={{ fontSize: 11.5, color: statusColor, fontWeight: 600, minWidth: 66, textAlign: "right" }}>{p.status}</span>
                        {p.status === "proposed" && (
                          <span style={{ display: "flex", gap: 6 }}>
                            <button onClick={() => review(p.id, "promote")} className="ao-h-b3"
                              style={{ padding: "5px 11px", borderRadius: 7, border: "1px solid rgba(52,211,153,.35)", background: "rgba(52,211,153,.1)", color: "var(--green)", fontSize: 11.5, fontWeight: 600, cursor: "pointer" }}>
                              Promote
                            </button>
                            <button onClick={() => review(p.id, "reject")} className="ao-h-b3"
                              style={{ padding: "5px 11px", borderRadius: 7, border: "1px solid rgba(248,113,113,.35)", background: "rgba(248,113,113,.08)", color: "var(--red-2)", fontSize: 11.5, fontWeight: 600, cursor: "pointer" }}>
                              Reject
                            </button>
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

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
                <MemoryPanel />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
