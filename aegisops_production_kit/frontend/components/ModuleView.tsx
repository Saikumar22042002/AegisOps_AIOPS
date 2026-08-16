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

type TelegramStatus = {
  channel: string; linked: boolean; enabled: boolean;
  // Which blocker is in the way when !enabled: the flag, or a missing bot token. They need
  // different operator actions, so the panel never collapses them into one message.
  reason?: "flag_off" | "no_token" | null;
  account?: string | null; linked_at?: string | null; linked_by?: string | null;
  code_pending: boolean; code_expires_at?: string | null; bot_username?: string | null;
};

function countdown(iso?: string | null): string {
  if (!iso) return "";
  const left = Math.floor((new Date(iso).getTime() - Date.now()) / 1000);
  if (left <= 0) return "expired";
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

// GW-1: per-user identity — bind a Telegram account to THIS Keycloak user. The code is a
// one-time bearer secret shown exactly once (the API never re-serves it), so the countdown and
// the plaintext live only in this component's state.
interface BindingRow {
  purpose: string; governed: boolean; default_model: string;
  bound_model: string | null; effective_model: string; eval_state: string | null;
  updated_by: string | null; reason: string | null;
}

interface PackRow {
  pack: string; provider: string; domain: string; configured: boolean;
  read: string[]; mutation: string[]; templates: string[]; day2: string[];
}

function CapabilitiesPanel() {
  // P4: the multi-cloud capability parity matrix — what AegisOps can do across AWS/Azure/
  // GCP/K8s/GitHub, provider-neutral. An unconfigured provider lists honestly (no fake
  // support). `packs_enabled` reflects the dark-launch flag.
  const [packs, setPacks] = useState<PackRow[] | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [posture, setPosture] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    api.get<{ packs: PackRow[]; packs_enabled: boolean; posture?: Record<string, string> }>("/capabilities")
      .then((r) => { setPacks(r.packs); setEnabled(r.packs_enabled); setPosture(r.posture ?? null); })
      .catch(() => setPacks(null));
  }, []);
  if (packs === null) return null;
  return (
    <div style={{ marginTop: 26 }} data-testid="capabilities">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Capability packs</span>
        <span style={{ fontSize: 11, color: "var(--text-4)" }}>
          multi-cloud parity · AWS · Azure · GCP · K8s · GitHub · {enabled ? "harness read path ON" : "dark (flag off)"}
        </span>
      </div>
      {posture && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }} data-testid="posture">
          {[["approval", posture.approval_model], ["mode", posture.permission_mode],
            ["cred broker", posture.credential_broker], ["durable engine", posture.durable_engine]]
            .map(([label, val]) => (
              <span key={label} style={{ fontSize: 10.5, color: "var(--text-3)", padding: "3px 9px",
                     borderRadius: 6, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
                {label}: <b style={{ color: "var(--text-2)" }}>{val}</b>
              </span>
            ))}
        </div>
      )}
      <div style={{ border: "1px solid var(--border)", borderRadius: 14, background: "var(--surface)", padding: "6px 18px" }}>
        {packs.map((p) => (
          <div key={p.pack} data-testid={`pack-${p.pack}`}
            style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
            <span style={{ width: 7, height: 7, borderRadius: 99, flexShrink: 0,
                           background: p.configured ? "var(--green)" : "var(--text-4)" }}
              title={p.configured ? "provider configured" : "no credentials — capability listed, not callable"} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <span style={{ fontSize: 12.5, color: "var(--text)", fontFamily: "'IBM Plex Mono',monospace" }}>{p.pack}</span>
              <div style={{ fontSize: 10.5, color: "var(--text-4)" }}>
                read: {p.read.join(", ") || "—"}{p.mutation.length ? ` · mutation: ${p.mutation.join(", ")}` : ""}
              </div>
            </div>
            <span style={{ fontSize: 10.5, color: "var(--text-4)" }}>{p.configured ? "configured" : "not configured"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ModelRoutingPanel() {
  // P1.7: org-level model bindings — which model serves each purpose. models.yaml says
  // what CAN run; these rows say what THIS org runs. Writes are server-side admin-gated
  // (403 surfaces inline) and every change lands an audit row.
  const [rows, setRows] = useState<BindingRow[] | null>(null);
  const [models, setModels] = useState<{ id: string; provider: string }[]>([]);
  const [err, setErr] = useState<string>("");
  const [busy, setBusy] = useState<string>("");

  const load = () =>
    Promise.allSettled([
      api.get<{ bindings: BindingRow[] }>("/models/bindings"),
      api.get<{ models: { id: string; provider: string; enabled: boolean }[] }>("/models"),
    ]).then(([b, m]) => {
      setRows(b.status === "fulfilled" ? b.value.bindings : null);
      setModels(m.status === "fulfilled" ? m.value.models.filter((x) => x.enabled) : []);
    });
  useEffect(() => { load(); }, []);

  const bind = async (purpose: string, model: string) => {
    setErr(""); setBusy(purpose);
    try { await api.put(`/models/bindings/${purpose}`, { model, reason: "set via Settings" }); await load(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
    finally { setBusy(""); }
  };
  const reset = async (purpose: string) => {
    setErr(""); setBusy(purpose);
    try { await api.del(`/models/bindings/${purpose}`); await load(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
    finally { setBusy(""); }
  };

  return (
    <div style={{ marginTop: 26 }} data-testid="model-routing">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Model routing</span>
        <span style={{ fontSize: 11, color: "var(--text-4)" }}>
          which model serves each purpose · governed purposes ignore per-run picks · changes are audited
        </span>
      </div>
      {err && <div style={{ fontSize: 12, color: "var(--red-2)", marginBottom: 10 }} data-testid="binding-error">{err}</div>}
      <div style={{ border: "1px solid var(--border)", borderRadius: 14, background: "var(--surface)", padding: "6px 18px" }}>
        {rows === null && (
          <div style={{ padding: "14px 0", fontSize: 12, color: "var(--text-4)" }}>
            model routing unavailable (GET /models/bindings)
          </div>
        )}
        {rows?.map((r) => (
          <div key={r.purpose} data-testid={`binding-${r.purpose}`}
            style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <span style={{ fontSize: 12.5, color: "var(--text)", fontFamily: "'IBM Plex Mono',monospace" }}>{r.purpose}</span>
              {r.governed && (
                <span title="Governed purpose: never user-pinnable, never silent-fallback"
                  style={{ marginLeft: 8, fontSize: 10, color: "var(--amber)", border: "1px solid var(--border-2)", borderRadius: 5, padding: "1px 6px" }}>
                  governed
                </span>
              )}
              <div style={{ fontSize: 10.5, color: "var(--text-4)" }}>
                default {r.default_model}
                {r.bound_model && r.eval_state ? ` · eval: ${r.eval_state}` : ""}
                {r.updated_by ? ` · by ${r.updated_by}` : ""}
              </div>
            </div>
            <select
              value={r.bound_model ?? r.default_model}
              disabled={busy === r.purpose}
              onChange={(e) => void bind(r.purpose, e.target.value)}
              data-testid={`binding-select-${r.purpose}`}
              style={{ fontSize: 12, padding: "5px 8px", borderRadius: 7, border: "1px solid var(--border-2)", background: "var(--surface-2)", color: "var(--text-2)" }}
            >
              {!models.some((m) => m.id === (r.bound_model ?? r.default_model)) && (
                <option value={r.bound_model ?? r.default_model}>{r.bound_model ?? r.default_model}</option>
              )}
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.id} ({m.provider})</option>
              ))}
            </select>
            {r.bound_model && (
              <button onClick={() => void reset(r.purpose)} disabled={busy === r.purpose} className="ao-h-b3"
                data-testid={`binding-reset-${r.purpose}`}
                style={{ padding: "5px 11px", borderRadius: 7, border: "1px solid var(--border-2)", background: "transparent", color: "var(--text-3)", fontSize: 11.5, cursor: "pointer" }}>
                Reset
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function ConnectedAccounts() {
  const [st, setSt] = useState<TelegramStatus | null>(null);
  const [code, setCode] = useState<string>("");
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  const [left, setLeft] = useState<string>("");
  const [err, setErr] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const load = () =>
    api.get<TelegramStatus>("/gateways/telegram").then(setSt).catch(() => setSt(null));
  useEffect(() => { load(); }, []);

  // One ticking countdown for whichever expiry is live (a freshly issued code, or a pending
  // one the API reports after a page refresh).
  const expiry = expiresAt ?? st?.code_expires_at ?? null;
  useEffect(() => {
    if (!expiry) { setLeft(""); return; }
    const tick = () => {
      const v = countdown(expiry);
      setLeft(v);
      if (v === "expired") { setCode(""); setExpiresAt(null); load(); }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiry]);

  const generate = async () => {
    setErr(""); setBusy(true);
    try {
      const d = await api.post<{ code: string; expires_at: string }>("/gateways/telegram/code");
      setCode(d.code); setExpiresAt(d.expires_at);
      load();
    } catch (e: any) { setErr(String(e?.message ?? e)); }
    finally { setBusy(false); }
  };

  const unlink = async () => {
    setErr(""); setBusy(true);
    try { await api.del("/gateways/telegram"); setCode(""); setExpiresAt(null); load(); }
    catch (e: any) { setErr(String(e?.message ?? e)); }
    finally { setBusy(false); }
  };

  const dot = st?.linked ? "var(--green)" : st?.enabled ? "var(--amber)" : "var(--text-4)";
  const label = st?.linked ? "linked"
    : st?.enabled ? "not linked"
    : st?.reason === "no_token" ? "no bot token"
    : "disabled";
  // The bot token is an OPERATOR secret and is never shown here (no route returns it) — the
  // panel only ever reports whether one is configured, and names the file to put it in.
  const blocked = st?.reason === "no_token"
    ? "AEGISOPS_TELEGRAM is on but TELEGRAM_BOT_TOKEN is empty. Paste the token from @BotFather into .env and restart the API — the token is never shown or stored here."
    : "Not enabled on this deployment (set AEGISOPS_TELEGRAM=on and TELEGRAM_BOT_TOKEN).";

  return (
    <div style={{ marginTop: 26 }} data-testid="connected-accounts">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text)" }}>Connected accounts</span>
        <span style={{ fontSize: 11, color: "var(--text-4)" }}>
          your identity on other channels · your roles, org and approval rules follow the link
        </span>
      </div>
      {err && <div style={{ fontSize: 12, color: "var(--red-2)", marginBottom: 10 }}>{err}</div>}
      <div style={{ border: "1px solid var(--border)", borderRadius: 14, background: "var(--surface)", padding: "16px 18px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ width: 34, height: 34, borderRadius: 9, background: "var(--surface-2)", border: "1px solid var(--border)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15, flexShrink: 0 }}>✈</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, color: "var(--text)", fontWeight: 500 }}>
              Telegram{st?.bot_username ? ` · @${st.bot_username}` : ""}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-4)" }}>
              {st?.linked
                ? `${st.account ?? "account"} · linked ${st.linked_at ? new Date(st.linked_at).toLocaleString() : ""}`
                : st?.enabled
                  ? "Message AegisOps from your phone. Unlinked senders get no access."
                  : blocked}
            </div>
          </div>
          <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 10.5, color: dot }}>
            <span style={{ width: 6, height: 6, borderRadius: 99, background: dot }} />{label}
          </span>
          {st?.linked ? (
            <button onClick={unlink} disabled={busy} className="ao-h-b3" data-testid="telegram-unlink"
              style={{ padding: "6px 13px", borderRadius: 8, border: "1px solid rgba(248,113,113,.35)", background: "rgba(248,113,113,.08)", color: "var(--red-2)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
              Unlink
            </button>
          ) : (
            <button onClick={generate} disabled={busy || !st?.enabled} className="ao-h-b3" data-testid="telegram-generate"
              style={{ padding: "6px 13px", borderRadius: 8, border: "1px solid rgba(129,140,248,.3)", background: "rgba(99,102,241,.1)", color: "var(--accent-fg)", fontSize: 12, fontWeight: 600, cursor: st?.enabled ? "pointer" : "not-allowed", opacity: st?.enabled ? 1 : 0.5 }}>
              Generate code
            </button>
          )}
        </div>

        {code && (
          <div data-testid="telegram-code" style={{ marginTop: 14, padding: "13px 15px", borderRadius: 10, border: "1px dashed var(--border-2)", background: "var(--surface-2)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 19, letterSpacing: ".14em", color: "var(--accent-3)", fontWeight: 600 }}>{code}</span>
              <span style={{ fontSize: 11.5, color: left === "expired" ? "var(--red-2)" : "var(--amber)" }}>
                expires in {left}
              </span>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 9, lineHeight: 1.6 }}>
              Send <code style={{ fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-2)" }}>/link {code}</code> to the AegisOps bot
              {st?.bot_username ? <> (<span style={{ color: "var(--text-2)" }}>@{st.bot_username}</span>)</> : null}.
              Single-use. Shown once — generate a new one if you lose it.
            </div>
          </div>
        )}

        {!code && st?.code_pending && (
          <div style={{ marginTop: 12, fontSize: 11.5, color: "var(--text-4)" }}>
            A code is already live (expires in {left}) but is shown only once — generate a new one to replace it.
          </div>
        )}
      </div>
    </div>
  );
}

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
  const isSettings = activeNav === "settings";

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

            {activeNav === "infrastructure" && <CapabilitiesPanel />}

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

            {/* GW-1: the Link Telegram control lives here — Settings → Connected accounts. */}
            {isSettings && <ModelRoutingPanel />}
            {isSettings && <ConnectedAccounts />}
          </>
        )}
      </div>
    </div>
  );
}
