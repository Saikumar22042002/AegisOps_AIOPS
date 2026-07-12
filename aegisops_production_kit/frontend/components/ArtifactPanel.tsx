"use client";

import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { artifactTabList, artifactTitles } from "../lib/data";
import { Icon } from "../lib/icons";
import { useUI } from "../lib/store";
import { tabStyle } from "../lib/styles";
import type { ArtifactTab } from "../lib/types";

const eyebrow: React.CSSProperties = { fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--text-4)", fontWeight: 600 };
const hdrBtn: React.CSSProperties = { width: 30, height: 30, borderRadius: 8, border: "1px solid var(--border-2)", background: "transparent", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", color: "var(--text-3)" };

export function ArtifactPanel() {
  const activeArtifact = useUI((s) => s.activeArtifact);
  const openArtifact = useUI((s) => s.openArtifact);
  const closeArtifact = useUI((s) => s.closeArtifact);
  const nonce = useUI((s) => s.artifactNonce);

  // The panel ALWAYS reflects the run of the currently selected/active message — never a stale
  // "latest run". `selectedMessageId` pins a specific message (its own run); with none set it
  // follows the newest run in the thread. Returns existing message objects (stable identity),
  // so unrelated state changes don't re-render and a static selection doesn't refetch.
  const panelMsg = useUI((s) => {
    const explicit = s.selectedMessageId ? s.messages.find((m) => m.id === s.selectedMessageId) : null;
    if (explicit) return explicit;
    return [...s.messages].reverse().find((m) => m.isAI && (m.runId || m.streaming)) ?? null;
  });
  const runId = panelMsg?.runId ?? null;
  const isLive = !!panelMsg?.streaming;
  const liveSteps = panelMsg?.steps ?? null;
  // Live console lines streamed for the selected run (Logs overlays these on the snapshot).
  const liveConsole = panelMsg?.consoleLines ?? null;

  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    // A new selection/run — drop the previous run's data immediately so the panel is never
    // shown bound to a stale run while the new fetch is in flight.
    setData(null);
    if (!runId) { setLoading(false); return; }
    setLoading(true);
    api.get(`/runs/${runId}/${activeArtifact}`)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // runId changes when a different message is selected; `nonce` bumps on run start/interrupt/
    // done/approval -> refetch that run's persisted artifacts.
  }, [runId, activeArtifact, nonce]);

  return (
    <aside id="ao-panel" style={{ width: 540, flexShrink: 0, background: "var(--bg-elev)", borderLeft: "1px solid var(--surface-3)", display: "flex", flexDirection: "column", height: "100%", animation: "ao-slidein .25s ease" }}>
      <div style={{ flexShrink: 0, borderBottom: "1px solid var(--surface-3)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 11, padding: "15px 18px 13px" }}>
          <span style={eyebrow}>Artifact</span>
          <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{artifactTitles[activeArtifact]}</span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <button onClick={closeArtifact} className="ao-h-s3" style={hdrBtn}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" /></svg>
            </button>
          </div>
        </div>
        <div style={{ display: "flex", gap: 2, padding: "0 10px", overflowX: "auto" }}>
          {artifactTabList.map(([key, label]) => (
            <button key={key} onClick={() => openArtifact(key as ArtifactTab)} style={tabStyle(activeArtifact === key)}>{label}</button>
          ))}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: "auto", minHeight: 0, padding: 18 }}>
        {(() => {
          // While the selected run is LIVE, the Timeline streams from the graph's step events in
          // real time (the persisted node view isn't complete until the run finishes).
          if (activeArtifact === "timeline" && isLive) {
            return (liveSteps?.length ?? 0) > 0 ? <LiveTimeline steps={liveSteps!} /> : <Empty msg="Starting run…" />;
          }
          // Logs: overlay the real streamed console lines (apply/plan output) on top of the
          // DB-derived snapshot so the tab reflects the live run, not just persisted summary.
          const effective =
            activeArtifact === "logs" && liveConsole && liveConsole.length
              ? { lines: [
                  ...((data?.lines as any[]) ?? []),
                  ...liveConsole.map((c) => ({
                    ts: "", lvl: c.stream === "stderr" ? "ERR" : "OUT",
                    lvlColor: c.stream === "stderr" ? "var(--red)" : "var(--cyan)", msg: c.line,
                  })),
                ] }
              : data;
          if (!runId) return <Empty msg="Run a request in the workspace, or select a message to view its run." />;
          if (loading && !effective) return <Empty msg="Loading…" />;
          if (!effective) return <Empty msg="No data for this artifact yet." />;
          return <TabBody tab={activeArtifact} data={effective} />;
        })()}
      </div>
    </aside>
  );
}

function Empty({ msg }: { msg: string }) {
  return <div style={{ fontSize: 13, color: "var(--text-4)", padding: "20px 2px" }}>{msg}</div>;
}

function TabBody({ tab, data }: { tab: ArtifactTab; data: any }) {
  if (tab === "timeline") return <Timeline data={data} />;
  if (tab === "terraform") return <Terraform data={data} />;
  if (tab === "reasoning") return <Reasoning data={data} />;
  if (tab === "logs") return <Logs data={data} />;
  if (tab === "metrics") return <Metrics data={data} />;
  if (tab === "traces") return <Traces data={data} />;
  if (tab === "references") return <References data={data} />;
  if (tab === "approvals") return <Approvals data={data} />;
  return null;
}

function statusColor(s: string) {
  return { done: "var(--green)", running: "var(--accent-2)", pending: "var(--amber)", rejected: "var(--red)", failed: "var(--red)", cancelled: "var(--border-2)" }[s] || "var(--border-3)";
}

// Real-time timeline for an in-flight run, rendered from the graph's streamed step events.
// Same node/connector visual as the persisted <Timeline>, so the handoff at `done` is seamless.
function LiveTimeline({ steps }: { steps: { label: string }[] }) {
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
        <span style={eyebrow}>LangGraph execution</span>
        <span style={{ marginLeft: "auto", fontSize: 11, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-3)" }}>running</span>
      </div>
      <div style={{ paddingLeft: 4 }}>
        {steps.map((st, i) => {
          const last = i === steps.length - 1;
          return (
            <div key={i} style={{ display: "flex", gap: 14, position: "relative", paddingBottom: 18 }}>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0 }}>
                <span style={{ width: 24, height: 24, borderRadius: 99, background: "var(--surface-3)", border: `1.5px solid ${last ? "var(--accent-2)" : "var(--green)"}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                  {last ? <Icon kind="spin" color="var(--accent-2)" /> : <Icon kind="check" color="var(--green)" />}
                </span>
                {!last && <span style={{ width: 1.5, flex: 1, background: "var(--border-2)", marginTop: 2, minHeight: 16 }} />}
              </div>
              <div style={{ paddingTop: 2, flex: 1 }}>
                <div style={{ fontSize: 13.5, color: "var(--text)", fontWeight: 500 }}>{st.label}</div>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function Timeline({ data }: { data: any }) {
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
        <span style={eyebrow}>LangGraph execution</span>
        <span style={{ marginLeft: "auto", fontSize: 11, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-3)" }}>{[data.elapsed, data.total, data.mode].filter(Boolean).join(" · ")}</span>
      </div>
      <div style={{ paddingLeft: 4 }}>
        {(data.nodes ?? []).map((n: any, i: number) => (
          <div key={i} style={{ display: "flex", gap: 14, position: "relative", paddingBottom: 18 }}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0 }}>
              <span style={{ width: 24, height: 24, borderRadius: 99, background: "var(--surface-3)", border: `1.5px solid ${statusColor(n.status)}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                {n.status === "done" ? <Icon kind="check" color="var(--green)" /> : n.status === "running" ? <Icon kind="spin" color="var(--accent-2)" /> : n.status === "rejected" || n.status === "failed" ? <Icon kind="x" color="var(--red)" /> : <span style={{ width: 7, height: 7, borderRadius: 99, background: statusColor(n.status) }} />}
              </span>
              {!n.last && <span style={{ width: 1.5, flex: 1, background: "var(--border-2)", marginTop: 2, minHeight: 16 }} />}
            </div>
            <div style={{ paddingTop: 2, flex: 1 }}>
              <div style={{ fontSize: 13.5, color: "var(--text)", fontWeight: 500 }}>{n.title}</div>
              <div style={{ fontSize: 12, color: "var(--text-4)", marginTop: 2 }}>{n.detail}</div>
            </div>
            <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 10.5, color: "var(--text-5)", paddingTop: 4 }}>{n.time}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function Terraform({ data }: { data: any }) {
  const s = data.summary ?? {};
  const checks = data.policy_checks ?? [];
  // P8 honesty: a check with passed===null / evaluated===false is NOT a pass — it's "not
  // evaluated" (the module enforces it but the policy engine doesn't verify it against the plan
  // yet; real in Phase 2). Count only genuinely-evaluated checks; never inflate the pass tally.
  const isNotEval = (p: any) => p.evaluated === false || p.passed === null || p.passed === undefined;
  const evaluated = checks.filter((p: any) => !isNotEval(p));
  const passed = evaluated.filter((p: any) => p.passed === true).length;
  const failed = evaluated.filter((p: any) => p.passed === false).length;
  const pending = checks.length - evaluated.length;
  const policyColor = failed > 0 ? "var(--red)" : "var(--green)";
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 11, marginBottom: 16 }}>
        <Card label="Plan"><span style={{ display: "flex", gap: 7, fontFamily: "'IBM Plex Mono',monospace", fontSize: 13 }}><span style={{ color: "var(--green)" }}>+{s.add ?? 0}</span><span style={{ color: "var(--amber)" }}>~{s.change ?? 0}</span><span style={{ color: "var(--text-4)" }}>-{s.destroy ?? 0}</span></span></Card>
        <Card label="Mode"><span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 13, color: "var(--text)" }}>{data.mode}</span></Card>
        <Card label="Policy"><span style={{ fontSize: 13, color: policyColor, fontWeight: 600, display: "flex", alignItems: "center", gap: 5 }}>
          {failed > 0 ? <Icon kind="x" color="#f87171" /> : <Icon kind="check" color="#34d399" />}
          {passed}/{evaluated.length} evaluated{pending > 0 ? ` · ${pending} pending` : ""}</span></Card>
      </div>
      {/* DEF: silently-defaulted dependency placements, stated explicitly — no invisible placement. */}
      {(data.defaults ?? []).length > 0 && (
        <div style={{ border: "1px solid rgba(251,191,36,.25)", borderRadius: 12, background: "rgba(251,191,36,.04)", padding: "11px 14px", marginBottom: 16 }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--amber)", fontWeight: 600, marginBottom: 7 }}>Defaults applied</div>
          {(data.defaults ?? []).map((d: any, i: number) => (
            <div key={i} style={{ fontSize: 12.5, color: "var(--text-2)", marginBottom: 3 }}>
              <span style={{ fontWeight: 500 }}>{d.name}:</span> <span style={{ fontFamily: "'IBM Plex Mono',monospace" }}>{d.value}</span>
              {d.note ? <span style={{ color: "var(--text-4)" }}> — {d.note}</span> : null}
            </div>
          ))}
        </div>
      )}
      <div style={{ border: "1px solid var(--border-2)", borderRadius: 12, background: "var(--code-bg)", marginBottom: 16, padding: "14px 15px", fontFamily: "'IBM Plex Mono',monospace", fontSize: 11.5, lineHeight: 1.7, overflowX: "auto" }}>
        {(data.diff ?? []).length === 0 ? <span style={{ color: "var(--text-4)" }}>No resource changes.</span> :
          (data.diff ?? []).map((d: any, i: number) => (
            <div key={i}><span style={{ color: d.sign === "+" ? "var(--green)" : d.sign === "-" ? "var(--red)" : "var(--amber)" }}>{d.sign}</span> <span style={{ color: "var(--accent-3)" }}>{d.type}</span> <span style={{ color: "var(--text-2)" }}>{d.address}</span></div>
          ))}
      </div>
      <div style={{ ...eyebrow, marginBottom: 11 }}>Policy checks</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {checks.map((p: any, i: number) => {
          const notEval = isNotEval(p);
          return (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 9, fontSize: 12.5 }}>
              {notEval
                ? <span title="not evaluated" style={{ width: 14, textAlign: "center", color: "var(--text-4)", fontWeight: 700 }}>–</span>
                : <Icon kind={p.passed ? "check" : "x"} color={p.passed ? "#34d399" : "#f87171"} />}
              <span style={{ color: notEval ? "var(--text-4)" : "var(--text-2)" }}>
                {p.name}{p.detail ? ` · ${p.detail}` : ""}{notEval ? " · not evaluated" : ""}
              </span>
            </div>
          );
        })}
      </div>
    </>
  );
}

function Reasoning({ data }: { data: any }) {
  const cards = data.cards ?? [];
  if (!cards.length) return <Empty msg="No reasoning recorded for this run." />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {cards.map((r: any, i: number) => (
        <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 12, padding: "13px 15px", background: "var(--surface)", borderLeft: "2px solid var(--accent-2)" }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text)", marginBottom: 5 }}>{r.title}</div>
          <div style={{ fontSize: 12.5, color: "var(--text-3)", lineHeight: 1.6 }}>{r.body}</div>
        </div>
      ))}
    </div>
  );
}

function Logs({ data }: { data: any }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 12, background: "var(--code-bg)", padding: "14px 15px", fontFamily: "'IBM Plex Mono',monospace", fontSize: 11.5, lineHeight: 1.85, overflowX: "auto" }}>
      {(data.lines ?? []).map((l: any, i: number) => (
        <div key={i} style={{ display: "flex", gap: 10, whiteSpace: "nowrap" }}><span style={{ color: "var(--text-5)" }}>{l.ts}</span><span style={{ color: l.lvlColor, minWidth: 42 }}>{l.lvl}</span><span style={{ color: "var(--text-2)" }}>{l.msg}</span></div>
      ))}
    </div>
  );
}

function Metrics({ data }: { data: any }) {
  return (
    <>
      <div style={{ ...eyebrow, marginBottom: 12 }}>Live metrics · {data.source}</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {(data.cards ?? []).map((m: any, i: number) => (
          <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 11, padding: "12px 13px", background: "var(--surface)" }}>
            <div style={{ fontSize: 10.5, color: "var(--text-4)", marginBottom: 5 }}>{m.label}</div>
            <div style={{ fontSize: 18, fontWeight: 600, color: "var(--text)", fontFamily: "'IBM Plex Mono',monospace" }}>{m.value}<span style={{ fontSize: 11, color: "var(--text-4)" }}>{m.unit}</span></div>
            <div style={{ fontSize: 10.5, color: m.subColor, marginTop: 3 }}>{m.sub}</div>
          </div>
        ))}
      </div>
    </>
  );
}

function Traces({ data }: { data: any }) {
  const spans = data.spans ?? [];
  const openInLangfuse = data.deep_link ? (
    <a href={data.deep_link} target="_blank" rel="noreferrer"
       style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "7px 13px", borderRadius: 8, border: "1px solid var(--border-3)", background: "var(--surface)", color: "var(--text)", fontSize: 12.5, fontWeight: 500, textDecoration: "none" }}>
      Open in Langfuse
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M7 17 17 7M9 7h8v8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </a>
  ) : null;
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
        <span style={eyebrow}>Langfuse trace</span>
        {data.total ? <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 10.5, color: "var(--text-4)" }}>{data.total}</span> : null}
        <span style={{ marginLeft: "auto", fontFamily: "'IBM Plex Mono',monospace", fontSize: 10.5, color: "var(--text-4)" }}>{String(data.trace_id ?? "").slice(0, 12)}</span>
      </div>
      {/* O1: the in-app tree is derived from the run's real run_steps (real durations). The full
          nested trace with tokens/cost lives in Langfuse — always deep-linked. When a run has no
          recorded steps we say so plainly rather than invent spans (P9 honesty preserved). */}
      {spans.length === 0 ? (
        <div style={{ border: "1px solid var(--border-2)", borderRadius: 12, background: "var(--surface-2)", padding: "16px 18px" }}>
          <div style={{ fontSize: 13, color: "var(--text-3)", lineHeight: 1.55 }}>
            {data.message ?? "No steps were recorded for this run. The full trace is available in Langfuse."}
          </div>
          {openInLangfuse ? <div style={{ marginTop: 12 }}>{openInLangfuse}</div> : (
            <div style={{ fontSize: 11.5, color: "var(--text-4)", marginTop: 10 }}>
              Set <code>LANGFUSE_HOST</code> to enable the deep-link. Trace id: <span style={{ fontFamily: "'IBM Plex Mono',monospace" }}>{data.trace_id}</span>
            </div>
          )}
        </div>
      ) : (
        <>
          {spans.map((sp: any, i: number) => (
            <div key={i} title={sp.error ?? undefined} style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 0", borderBottom: "1px solid var(--border)" }}>
              <span style={{ width: 7, height: 7, borderRadius: 99, background: sp.dot, marginLeft: sp.indent }} />
              <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12, color: sp.status === "failed" ? "var(--red)" : "var(--text-2)", flex: 1 }}>{sp.name}</span>
              <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 10.5, color: "var(--text-4)" }}>{sp.dur}</span>
            </div>
          ))}
          {openInLangfuse ? <div style={{ marginTop: 14 }}>{openInLangfuse}</div> : null}
        </>
      )}
    </>
  );
}

function References({ data }: { data: any }) {
  const refs = data.references ?? [];
  if (!refs.length) return <Empty msg="No references cited for this run." />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
      {refs.map((r: any, i: number) => (
        <div key={i} style={{ display: "flex", gap: 11, padding: 13, borderRadius: 11, border: "1px solid var(--border)", background: "var(--surface)" }}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0, marginTop: 1 }}><path d="M6 4h8l4 4v12H6V4Z" stroke="#a5b4fc" strokeWidth="1.5" strokeLinejoin="round" /></svg>
          <div><div style={{ fontSize: 12.5, color: "var(--text-2)", fontWeight: 500 }}>{r.title}</div><div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 2 }}>{r.source ?? r.kind ?? ""}{r.relevance != null ? ` · ${(r.relevance * 100).toFixed(0)}%` : ""}</div></div>
        </div>
      ))}
    </div>
  );
}

function Approvals({ data }: { data: any }) {
  const cell = (label: string, value: React.ReactNode, color = "var(--text)") => (
    <div style={{ background: "var(--bg-elev)", padding: "13px 15px" }}><div style={{ fontSize: 10.5, color: "var(--text-4)", marginBottom: 4 }}>{label}</div><div style={{ fontSize: 13, color }}>{value}</div></div>
  );
  return (
    <>
      <div style={{ ...eyebrow, marginBottom: 14 }}>Approval gate · {data.status}</div>
      <div style={{ border: "1px solid var(--border)", borderRadius: 13, overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1, background: "var(--border)" }}>
          {cell("Risk", <span style={{ color: "var(--amber)", fontWeight: 600 }}>{data.risk}</span>)}
          {cell("Affected", data.affected)}
          {cell("ServiceNow", <span style={{ color: "var(--accent-3)", fontFamily: "'IBM Plex Mono',monospace" }}>{data.servicenow ?? "—"}</span>)}
          {cell("Cost impact", data.cost_impact ?? "—")}
        </div>
      </div>
      <div style={{ ...eyebrow, margin: "14px 0 11px" }}>Decisions</div>
      {(data.decisions ?? []).length === 0 ? <Empty msg="No approval decision recorded yet." /> :
        (data.decisions ?? []).map((d: any, i: number) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 11, border: "1px solid var(--border)", borderRadius: 11, padding: "13px 15px", marginBottom: 8, background: "var(--surface)" }}>
            <Icon kind={d.decision === "approved" ? "check" : "x"} color={d.decision === "approved" ? "var(--green)" : "var(--red)"} />
            <div style={{ fontSize: 12.5, color: "var(--text-2)" }}>{d.decision} by <span style={{ color: "var(--text)", fontWeight: 500 }}>{d.actor}</span> ({d.role})</div>
          </div>
        ))}
    </>
  );
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return <div style={{ border: "1px solid var(--border)", borderRadius: 11, padding: "13px 14px", background: "var(--surface)" }}><div style={{ fontSize: 10.5, color: "var(--text-3)", marginBottom: 6 }}>{label}</div>{children}</div>;
}
