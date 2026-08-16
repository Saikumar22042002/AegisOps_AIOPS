"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { BrandShield } from "../lib/icons";
import { useUI } from "../lib/store";
import type { ChatMessage, ParamRequest } from "../lib/types";
import { Markdown } from "./Markdown";

export function Workspace() {
  const artifactOpen = useUI((s) => s.artifactOpen);
  const chatMaxWidth = artifactOpen ? "100%" : "780px";
  const messages = useUI((s) => s.messages);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 }}>
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        <div className="ao-chat-pad" style={{ maxWidth: chatMaxWidth, margin: "0 auto", padding: "40px 36px 28px", transition: "max-width .25s" }}>
          {messages.length === 0 ? <EmptyState /> : <Conversation />}
          <div style={{ height: 8 }} />
        </div>
      </div>
      <Composer chatMaxWidth={chatMaxWidth} />
    </div>
  );
}

function EmptyState() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "60px 0", textAlign: "center" }}>
      <div style={{ width: 54, height: 54, borderRadius: 15, background: "linear-gradient(155deg,var(--accent),var(--accent-strong))", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 18, boxShadow: "0 8px 24px rgba(79,70,229,.4)" }}>
        <BrandShield size={26} />
      </div>
      <div style={{ fontSize: 19, fontWeight: 600, color: "var(--text)", letterSpacing: "-.02em" }}>How can I help with your infrastructure?</div>
      <div style={{ fontSize: 13.5, color: "var(--text-3)", marginTop: 8, maxWidth: 440, lineHeight: 1.6 }}>
        Ask AegisOps to provision across AWS · Azure · GCP, deploy with DevOps pipelines, or investigate an incident. Every change runs through human approval.
      </div>
    </div>
  );
}

function Conversation() {
  const messages = useUI((s) => s.messages);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);
  return (
    <>
      {messages.map((m, i) => (
        <div key={m.id} style={{ marginTop: i === 0 ? 0 : 38, animation: "ao-fadeup .3s ease" }}>
          {m.isUser ? (
            <div style={{ display: "flex", gap: 15 }}>
              <div style={avatarUser}>MO</div>
              <div style={{ flex: 1, paddingTop: 4, fontSize: 15, color: "var(--text)", lineHeight: 1.65 }}>{m.text}</div>
            </div>
          ) : (
            <AiMessage m={m} />
          )}
        </div>
      ))}
      <div ref={endRef} />
    </>
  );
}

function AiMessage({ m }: { m: ChatMessage }) {
  const [tab, setTab] = useState<"conversation" | "analysis">("conversation");
  const approval = useUI((s) => s.approval);
  const approveRun = useUI((s) => s.approveRun);
  const sendText = useUI((s) => s.sendText);
  const openArtifact = useUI((s) => s.openArtifact);
  const selectMessage = useUI((s) => s.selectMessage);
  const selected = useUI((s) => s.selectedMessageId === m.id);
  const artifactOpen = useUI((s) => s.artifactOpen);
  const { user } = useAuth();
  const canApprove = !!user?.can_approve;
  const feedback = useUI((s) => s.feedback);
  const submitFeedback = useUI((s) => s.submitFeedback);
  const fbId = m.messageId || m.id;
  const fb = feedback[fbId];
  const plan = (m.interrupt?.plan as any) || null;
  const summary = plan?.summary;
  const conf = m.confidentiality;
  const confColor = conf?.level === "High" ? "var(--red)" : conf?.level === "Medium" ? "var(--amber)" : "var(--green)";

  return (
    <div
      onClick={() => selectMessage(m.id)}
      title="Show this message's run in the artifact panel"
      // Clicking any message pins the artifact panel to THAT message's run. The left accent
      // (same idiom as the sidebar's active row) marks which message the panel is bound to.
      // Left padding is always reserved so toggling the accent never shifts the layout.
      style={{ display: "flex", gap: 15, cursor: "pointer", marginLeft: -14, paddingLeft: 14,
               boxShadow: selected && artifactOpen ? "inset 2px 0 0 var(--accent-2)" : "none", transition: "box-shadow .15s" }}
    >
      <div style={avatarAI}><BrandShield size={15} filled={false} /></div>
      <div style={{ flex: 1, minWidth: 0, paddingTop: 2 }}>
        {/* meta chips */}
        {(m.intent || m.workflow || conf || m.servedBy) && (
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 14 }}>
            {m.intent && <Chip label="Intent" value={m.intent} />}
            {m.workflow && <Chip label="Workflow" value={m.workflow} mono />}
            {m.servedBy && (
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 500,
                         color: m.servedBy.fallbackHop > 0 ? "var(--amber)" : "var(--text-3)",
                         padding: "4px 10px", borderRadius: 7, background: "var(--surface-2)", border: "1px solid var(--border)" }}
                title={m.servedBy.fallbackHop > 0
                  ? `Served by ${m.servedBy.provider}/${m.servedBy.model} after ${m.servedBy.fallbackHop} fallback hop(s) — requested ${m.servedBy.requestedModel ?? "default"}`
                  : `Served by ${m.servedBy.provider}/${m.servedBy.model}`}
              >
                <span style={{ width: 6, height: 6, borderRadius: 99, background: "currentColor" }} />
                {m.servedBy.model}
                {m.servedBy.fallbackHop > 0 && ` · fallback ×${m.servedBy.fallbackHop}`}
              </span>
            )}
            {conf && (
              <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 500, color: confColor, padding: "4px 10px", borderRadius: 7, background: "var(--surface-2)", border: "1px solid var(--border)" }}
                title={`Confidentiality ${conf.level} · ${(conf.score * 100).toFixed(0)}%`}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><rect x="5" y="10" width="14" height="10" rx="2.5" stroke="currentColor" strokeWidth="1.7" /><path d="M8 10V7a4 4 0 0 1 8 0v3" stroke="currentColor" strokeWidth="1.7" /></svg>
                {conf.level}
              </span>
            )}
          </div>
        )}

        {/* two-tab */}
        <div style={{ display: "flex", gap: 16, marginBottom: 12, borderBottom: "1px solid var(--border)" }}>
          {(["conversation", "analysis"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              style={{ padding: "0 0 8px", border: "none", background: "transparent", cursor: "pointer", fontSize: 12.5, fontWeight: 500,
                       color: tab === t ? "var(--text)" : "var(--text-4)", boxShadow: tab === t ? "inset 0 -2px 0 var(--accent-2)" : "none" }}>
              {t === "conversation" ? "Conversation" : "Analysis / References"}
            </button>
          ))}
        </div>

        {tab === "conversation" ? (
          <>
            {/* live timeline while thinking */}
            {m.showTimeline && (m.steps?.length ?? 0) > 0 && (
              <div style={{ border: "1px solid rgba(129,140,248,.18)", borderRadius: 12, background: "rgba(99,102,241,.04)", padding: "14px 16px", marginBottom: 16 }}>
                <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".07em", color: "var(--accent-2)", fontWeight: 600, marginBottom: 11 }}>AI activity</div>
                {(m.steps ?? []).map((st, i) => {
                  const active = i === (m.stepIdx ?? 0) && m.streaming;
                  return (
                    <div key={i} style={{ display: "flex", alignItems: "center", gap: 11, padding: "4px 0", animation: "ao-stepin .25s ease" }}>
                      {active ? (
                        <span style={{ width: 17, height: 17, borderRadius: 99, background: "rgba(129,140,248,.16)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" style={{ animation: "ao-spin 1s linear infinite" }}><path d="M12 3a9 9 0 1 0 9 9" stroke="#818cf8" strokeWidth="2.6" strokeLinecap="round" /></svg>
                        </span>
                      ) : (
                        <span style={{ width: 17, height: 17, borderRadius: 99, background: "rgba(52,211,153,.13)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none"><path d="m5 12 5 5 9-11" stroke="#34d399" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
                        </span>
                      )}
                      <span style={{ fontSize: 13, color: "var(--text-2)" }}>{st.label}</span>
                    </div>
                  );
                })}
              </div>
            )}
            {/* collapsed activity after done */}
            {!m.showTimeline && (m.steps?.length ?? 0) > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 13px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-3)", fontSize: 12.5, marginBottom: 16, width: "fit-content" }}>
                <span style={{ width: 16, height: 16, borderRadius: 99, background: "rgba(52,211,153,.14)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none"><path d="m5 12 5 5 9-11" stroke="#34d399" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </span>
                <span style={{ color: "var(--text-2)", fontWeight: 500 }}>AI activity</span>
                <span>· {m.steps?.length} steps</span>
              </div>
            )}

            {/* Assistant text renders as full markdown (N-04) — never literal **markers**. */}
            <div>
              <Markdown text={m.text} />
              {m.streaming && <span style={{ display: "inline-block", width: 7, height: 16, background: "var(--accent-2)", marginLeft: 2, verticalAlign: -2, animation: "ao-blink 1s steps(1) infinite" }} />}
            </div>

            {m.done && (m.sensitiveOutputs?.length ?? 0) > 0 && m.runId && (
              <CredentialReveal runId={m.runId} outputs={m.sensitiveOutputs!} />
            )}

            {m.error && (
              <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--red-2)", background: "rgba(248,113,113,.08)", border: "1px solid rgba(248,113,113,.25)", borderRadius: 10, padding: "10px 13px" }}>
                {m.error}
                {/* U7: one-click retry-with-fix — re-sends the corrected message as a real new turn. */}
                {m.retry?.retry_message && (
                  <div style={{ marginTop: 9 }}>
                    <button onClick={(e) => { e.stopPropagation(); void sendText(m.retry!.retry_message); }} className="ao-h-b3"
                      title={m.retry.retry_message}
                      style={{ padding: "6px 12px", borderRadius: 8, border: "1px solid rgba(129,140,248,.35)", background: "rgba(99,102,241,.12)", color: "var(--accent-fg)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
                      {m.retry.label ?? "Retry with fix"}
                    </button>
                  </div>
                )}
              </div>
            )}

            {m.paramRequest && (m.paramRequest.items?.length ?? 0) > 0 && <ParamRequestCard req={m.paramRequest} />}

            {/* artifact cards + approval gate when a plan exists */}
            {summary && (
              <div style={{ marginTop: 16 }}>
                <button onClick={() => openArtifact("terraform")} className="ao-h-tfcard"
                  style={{ display: "flex", alignItems: "center", gap: 14, padding: "15px 16px", borderRadius: 13, border: "1px solid var(--border-2)", background: "var(--surface)", cursor: "pointer", textAlign: "left", width: "100%", transition: "all .15s" }}>
                  <span style={{ width: 40, height: 40, borderRadius: 10, background: "rgba(129,140,248,.12)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="m3 8 9-5 9 5-9 5-9-5Z" stroke="#a5b4fc" strokeWidth="1.5" strokeLinejoin="round" /></svg>
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)", marginBottom: 3 }}>Terraform Plan</div>
                    <div style={{ fontSize: 12.5, color: "var(--text-3)" }}>
                      {String(m.interrupt?.workflow ?? "")} · {m.decision === "approved" ? (m.done ? "applied" : "applying") : m.decision === "rejected" ? "rejected" : "approval required"}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                    <span style={chipMono("rgba(52,211,153,.12)", "var(--green)")}>+{summary.add}</span>
                    <span style={chipMono("rgba(251,191,36,.12)", "var(--amber)")}>~{summary.change}</span>
                    <span style={chipMono("var(--surface-3)", "var(--text-4)")}>-{summary.destroy}</span>
                  </div>
                </button>

                {/* U6: goal-DAG card — one approval covers every ordered step. Each step shows
                    its real plan summary, or states honestly that it plans at execute time
                    (its inputs are wired to a parent's outputs that don't exist yet). */}
                {Array.isArray(plan?.steps) && plan.steps.length > 0 && (
                  <div style={{ marginTop: 11, border: "1px solid var(--border-2)", borderRadius: 12, background: "var(--surface)", padding: "12px 14px" }}>
                    <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".07em", color: "var(--accent-2)", fontWeight: 600, marginBottom: 9 }}>
                      Goal plan · {plan.steps.length} steps · one approval
                    </div>
                    {plan.steps.map((st: any) => (
                      <div key={st.order} style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 0", borderBottom: "1px solid var(--border)", fontSize: 12.5 }}>
                        <span style={{ width: 18, height: 18, borderRadius: 99, background: "var(--surface-3)", color: "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10.5, fontFamily: "'IBM Plex Mono',monospace", flexShrink: 0 }}>{st.order}</span>
                        <span style={{ fontFamily: "'IBM Plex Mono',monospace", color: "var(--accent-3)" }}>{st.template}</span>
                        <span style={{ color: "var(--text-2)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{st.name}</span>
                        {typeof st.plan === "object" && st.plan ? (
                          <span style={chipMono("rgba(52,211,153,.12)", "var(--green)")}>+{st.plan.add ?? 0}</span>
                        ) : (
                          <span style={{ fontSize: 11, color: "var(--text-4)" }} title={String(st.plan ?? "")}>plans after parent</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* P0-3: instant, in-place feedback the moment Approve & apply is clicked —
                    the decision card is replaced by a live applying strip; per-step progress
                    streams in the AI-activity block above and the console in the Logs tab. */}
                {m.decision === "approved" && m.streaming && (
                  <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "13px 16px", borderRadius: 12, border: "1px solid rgba(52,211,153,.25)", background: "rgba(52,211,153,.05)", marginTop: 11 }}>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" style={{ animation: "ao-spin 1s linear infinite", flexShrink: 0 }}><path d="M12 3a9 9 0 1 0 9 9" stroke="var(--green)" strokeWidth="2.6" strokeLinecap="round" /></svg>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 500, color: "var(--text)" }}>Approved — applying now</div>
                      <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                        {m.steps?.length ? `Step ${m.steps.length}: ${m.steps[m.steps.length - 1]?.label}` : "Starting the apply…"} · console in the Logs tab
                      </div>
                    </div>
                  </div>
                )}
                {m.decision === "rejected" && !m.done && (
                  <div style={{ padding: "10px 16px", borderRadius: 12, border: "1px solid var(--border-2)", background: "var(--surface)", marginTop: 11, fontSize: 12.5, color: "var(--text-3)" }}>
                    Rejected — nothing was changed.
                  </div>
                )}
                {approval === "pending" && !m.decision && (
                  <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "13px 16px", borderRadius: 12, border: "1px solid rgba(251,191,36,.25)", background: "rgba(251,191,36,.05)", marginTop: 11 }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 13.5, fontWeight: 500, color: "var(--text)" }}>Human approval required</div>
                      <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                        {canApprove ? "Review the plan, then approve to apply." : "Requires Cloud Architect, Org Admin, or Platform Admin to approve."}
                      </div>
                    </div>
                    {canApprove ? (
                      <>
                        <button onClick={() => approveRun("approved")} className="ao-h-bright" style={{ padding: "8px 15px", borderRadius: 9, border: "none", background: "var(--green-strong)", color: "var(--on-green)", fontSize: 12.5, fontWeight: 600, cursor: "pointer" }}>Approve &amp; apply</button>
                        <button onClick={() => approveRun("rejected")} className="ao-h-reject" style={{ padding: "8px 14px", borderRadius: 9, border: "1px solid var(--border-3)", background: "transparent", color: "var(--text-2)", fontSize: 12.5, fontWeight: 500, cursor: "pointer" }}>Reject</button>
                      </>
                    ) : (
                      <span style={{ fontSize: 11, fontWeight: 500, color: "var(--amber)", padding: "5px 11px", borderRadius: 7, background: "rgba(251,191,36,.1)", border: "1px solid rgba(251,191,36,.25)" }}>Approver role required</span>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* feedback */}
            {m.done && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 16, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
                <span style={{ fontSize: 11.5, color: "var(--text-4)" }}>Was this helpful?</span>
                <button onClick={() => submitFeedback(fbId, "up")} className="ao-h-b3" style={fbBtn(fb === "up" ? "rgba(52,211,153,.4)" : "var(--border-2)", fb === "up" ? "rgba(52,211,153,.12)" : "var(--surface-2)", fb === "up" ? "var(--green)" : "var(--text-3)")}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M7 10v9H4v-9h3Zm0 0 4.5-7c1 0 2 .8 2 2v3h4.6c1.2 0 2 1 1.8 2.2l-1.3 6c-.2.9-1 1.6-2 1.6H7" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
                </button>
                <button onClick={() => submitFeedback(fbId, "down")} className="ao-h-b3" style={fbBtn(fb === "down" ? "rgba(248,113,113,.4)" : "var(--border-2)", fb === "down" ? "rgba(248,113,113,.1)" : "var(--surface-2)", fb === "down" ? "var(--red-2)" : "var(--text-3)")}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M17 14V5h3v9h-3Zm0 0-4.5 7c-1 0-2-.8-2-2v-3H5.9c-1.2 0-2-1-1.8-2.2l1.3-6c.2-.9 1-1.6 2-1.6H17" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
                </button>
                {m.runId && <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-4)", fontFamily: "'IBM Plex Mono',monospace" }}>ctx {m.runId.slice(0, 8)}</span>}
              </div>
            )}
          </>
        ) : (
          <AnalysisTab m={m} />
        )}
      </div>
    </div>
  );
}

function AnalysisTab({ m }: { m: ChatMessage }) {
  const cards = m.analysis?.cards ?? [];
  const refs = m.references ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {m.analysis?.summary && <div style={{ fontSize: 13, color: "var(--text-3)", lineHeight: 1.7 }}>{m.analysis.summary}</div>}
      {cards.map((c, i) => (
        <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 12, padding: "13px 15px", background: "var(--surface)", borderLeft: "2px solid var(--accent-2)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 5 }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text)" }}>{c.title}</span>
            {c.conf && <span style={{ marginLeft: "auto", fontFamily: "'IBM Plex Mono',monospace", fontSize: 10, color: "var(--green)", padding: "2px 7px", borderRadius: 5, background: "rgba(52,211,153,.1)" }}>{c.conf}</span>}
          </div>
          <div style={{ fontSize: 12.5, color: "var(--text-3)", lineHeight: 1.6 }}>{c.body}</div>
        </div>
      ))}
      {refs.length > 0 && <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--text-4)", fontWeight: 600, marginTop: 6 }}>References</div>}
      {refs.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 11, padding: 12, borderRadius: 11, border: "1px solid var(--border)", background: "var(--surface)" }}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0, marginTop: 1 }}><path d="M6 4h8l4 4v12H6V4Z" stroke="#a5b4fc" strokeWidth="1.5" strokeLinejoin="round" /></svg>
          <div><div style={{ fontSize: 12.5, color: "var(--text-2)", fontWeight: 500 }}>{r.title}</div>
            <div style={{ fontSize: 11, color: "var(--text-4)", marginTop: 2 }}>{r.source ?? r.kind ?? ""}{r.relevance != null ? ` · ${(r.relevance * 100).toFixed(0)}% relevance` : ""}</div></div>
        </div>
      ))}
      {cards.length === 0 && refs.length === 0 && <div style={{ fontSize: 12.5, color: "var(--text-4)" }}>No analysis yet for this message.</div>}
    </div>
  );
}

function CredentialReveal({ runId, outputs }: { runId: string; outputs: string[] }) {
  // One-time credential reveal (N-02 + S1): the server serves each value exactly once; the value
  // lives only in this component's state — never persisted, never logged. Revealing requires a
  // step-up re-auth (password re-entry) which the modal below collects.
  const [state, setState] = useState<Record<string, { value?: string; error?: string; busy?: boolean }>>({});
  const [stepUp, setStepUp] = useState<{ name: string; password: string; error?: string; busy?: boolean } | null>(null);

  const submitReveal = async () => {
    if (!stepUp) return;
    const name = stepUp.name;
    setStepUp((s) => (s ? { ...s, busy: true, error: undefined } : s));
    try {
      const r = await api.post<{ value: string }>(`/runs/${runId}/credentials`,
        { output: name, password: stepUp.password });
      setState((s) => ({ ...s, [name]: { value: r.value } }));
      setStepUp(null);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 401) {
        // Fresh-auth failed — keep the modal open with a clear message.
        setStepUp((s) => (s ? { ...s, busy: false, password: "", error: "That didn't re-authenticate you. Re-enter your password to reveal this credential." } : s));
        return;
      }
      // 404 / 410 / 5xx are terminal for this attempt — surface on the row, close the modal.
      setState((s) => ({ ...s, [name]: { error: e instanceof Error ? e.message : "reveal failed" } }));
      setStepUp(null);
    }
  };

  const download = (name: string, value: string) => {
    const blob = new Blob([value], { type: "application/x-pem-file" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name.includes("key") ? `${name}.pem` : `${name}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ marginTop: 14, border: "1px solid rgba(251,191,36,.25)", borderRadius: 12, background: "rgba(251,191,36,.04)", padding: "12px 15px" }}>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--amber)", fontWeight: 600, marginBottom: 9 }}>
        Credentials — one-time reveal
      </div>
      {outputs.map((name) => {
        const st = state[name] ?? {};
        return (
          <div key={name} style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 12.5, color: "var(--text-2)" }}>{name}</span>
              {!st.value && (
                <button onClick={() => setStepUp({ name, password: "" })} disabled={st.busy}
                  style={{ padding: "5px 11px", borderRadius: 8, border: "1px solid var(--border-3)", background: "var(--surface-2)", color: "var(--text)", fontSize: 12, fontWeight: 500, cursor: "pointer" }}>
                  {st.busy ? "Revealing…" : "Reveal credential"}
                </button>
              )}
              {st.value && (
                <>
                  <button onClick={() => void navigator.clipboard?.writeText(st.value!)}
                    style={{ padding: "5px 11px", borderRadius: 8, border: "1px solid var(--border-3)", background: "var(--surface-2)", color: "var(--text)", fontSize: 12, cursor: "pointer" }}>Copy</button>
                  <button onClick={() => download(name, st.value!)}
                    style={{ padding: "5px 11px", borderRadius: 8, border: "1px solid var(--border-3)", background: "var(--surface-2)", color: "var(--text)", fontSize: 12, cursor: "pointer" }}>Download</button>
                </>
              )}
            </div>
            {st.value && (
              <pre style={{ margin: 0, padding: "9px 11px", background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 9, overflowX: "auto", maxHeight: 140, fontSize: 11, fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-3)" }}>{st.value}</pre>
            )}
            {/* P1-3: the downloaded key is only usable after chmod 600 — say so here, once. */}
            {st.value && name.includes("key") && (
              <div style={{ fontSize: 11, color: "var(--text-4)" }}>
                After saving: <code style={{ fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-3)" }}>chmod 600 {name}.pem</code> — SSH refuses world-readable keys.
              </div>
            )}
            {st.error && <div style={{ fontSize: 11.5, color: "var(--amber)" }}>{st.error}</div>}
          </div>
        );
      })}
      <div style={{ fontSize: 11, color: "var(--text-4)" }}>
        Shown once, never stored by AegisOps. Save it now — a second reveal is refused.
      </div>
      {stepUp && (
        <div onClick={() => !stepUp.busy && setStepUp(null)}
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 60 }}>
          <div onClick={(e) => e.stopPropagation()}
            style={{ width: 380, maxWidth: "90vw", background: "var(--surface)", border: "1px solid var(--border-2)", borderRadius: 14, padding: "20px 22px", boxShadow: "0 18px 50px rgba(0,0,0,.4)" }}>
            <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--text)", marginBottom: 6 }}>Confirm it&apos;s you</div>
            <div style={{ fontSize: 12.5, color: "var(--text-3)", lineHeight: 1.5, marginBottom: 14 }}>
              Revealing <span style={{ fontFamily: "'IBM Plex Mono',monospace", color: "var(--text-2)" }}>{stepUp.name}</span> requires a fresh sign-in. Re-enter your password — this is logged.
            </div>
            <input type="password" autoFocus value={stepUp.password}
              onChange={(e) => setStepUp((s) => (s ? { ...s, password: e.target.value } : s))}
              onKeyDown={(e) => { if (e.key === "Enter" && stepUp.password && !stepUp.busy) void submitReveal(); }}
              placeholder="Your password"
              style={{ width: "100%", boxSizing: "border-box", padding: "10px 12px", borderRadius: 9, border: "1px solid var(--border-2)", background: "var(--surface-2)", color: "var(--text)", fontSize: 13.5, outline: "none" }} />
            {stepUp.error && <div style={{ fontSize: 11.5, color: "var(--amber)", marginTop: 9 }}>{stepUp.error}</div>}
            <div style={{ display: "flex", gap: 9, marginTop: 16, justifyContent: "flex-end" }}>
              <button onClick={() => setStepUp(null)} disabled={stepUp.busy}
                style={{ padding: "8px 14px", borderRadius: 8, border: "1px solid var(--border-2)", background: "transparent", color: "var(--text-3)", fontSize: 12.5, cursor: "pointer" }}>Cancel</button>
              <button onClick={() => void submitReveal()} disabled={!stepUp.password || stepUp.busy}
                style={{ padding: "8px 14px", borderRadius: 8, border: "none", background: stepUp.password && !stepUp.busy ? "var(--accent)" : "var(--border-2)", color: "#fff", fontSize: 12.5, fontWeight: 500, cursor: stepUp.password && !stepUp.busy ? "pointer" : "default" }}>
                {stepUp.busy ? "Verifying…" : "Confirm & reveal"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ParamRequestCard({ req }: { req: ParamRequest }) {
  // "Required inputs" card — same visual idiom as the analysis/artifact cards (design tokens only).
  return (
    <div style={{ marginTop: 14, border: "1px solid var(--border)", borderLeft: "2px solid var(--accent-2)", borderRadius: 12, background: "var(--surface)", padding: "14px 16px" }}>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--accent-2)", fontWeight: 600, marginBottom: 11 }}>Required to proceed</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 11 }}>
        {req.items.map((it) => (
          <div key={it.name} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{it.label}</span>
              <span style={{ fontSize: 10, color: "var(--amber)", fontWeight: 500, padding: "1px 7px", borderRadius: 99, background: "rgba(251,191,36,.12)" }}>required</span>
            </div>
            {it.help && <div style={{ fontSize: 12, color: "var(--text-3)", lineHeight: 1.5 }}>{it.help}</div>}
            {it.choices && it.choices.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 2 }}>
                {it.choices.map((ch) => (
                  <span key={ch} style={{ fontFamily: "'IBM Plex Mono',monospace", fontSize: 11, color: "var(--text-2)", padding: "2px 8px", borderRadius: 6, background: "var(--surface-2)", border: "1px solid var(--border)" }}>{ch}</span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: "var(--text-4)", marginTop: 12 }}>Reply with these values — e.g. “name web-01, t3.large, ubuntu, key my-key”.</div>
    </div>
  );
}

function Composer({ chatMaxWidth }: { chatMaxWidth: string }) {
  const input = useUI((s) => s.input);
  const queued = useUI((s) => s.queued);
  const setInput = useUI((s) => s.setInput);
  const sendText = useUI((s) => s.sendText);
  const streaming = useUI((s) => s.streaming);
  const model = useUI((s) => s.model);
  const { user } = useAuth();
  // S3: read-only roles cannot initiate a run. The backend enforces this (POST /chat →
  // 403); the composer is honest about it rather than letting the user type into a dead box.
  const canInitiate = user ? !!user.can_initiate : true;
  const canSend = !!input.trim() && !streaming && canInitiate;
  const suggestions = [
    "Provision an S3 bucket in AWS us-east-1",
    "Why did checkout latency spike after the 14:20 deploy?",
    "Create a GCS bucket in my GCP project",
  ];
  if (!canInitiate) {
    return (
      <div className="ao-composer-pad" style={{ flexShrink: 0, padding: "0 36px 26px" }}>
        <div style={{ maxWidth: chatMaxWidth, margin: "0 auto", transition: "max-width .25s" }}>
          <div style={{ border: "1px solid var(--border-2)", borderRadius: 16, background: "var(--surface-2)", padding: "16px 18px", display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ width: 30, height: 30, borderRadius: 8, background: "var(--surface)", border: "1px solid var(--border-2)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, color: "var(--text-4)" }}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M6 10V8a6 6 0 1 1 12 0v2m-9 0h6a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3Z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </span>
            <div style={{ fontSize: 13, color: "var(--text-3)", lineHeight: 1.5 }}>
              Your role is <b style={{ color: "var(--text-2)" }}>read-only</b> — you can view every conversation, run, and artifact in your organization, but not start a new request. Ask a Developer, DevOps Engineer, SRE, or an admin to initiate changes.
            </div>
          </div>
          <div style={{ textAlign: "center", fontSize: 11, color: "var(--text-5)", marginTop: 10 }}>
            {model} · Read-only access · Every destructive action requires an approver.
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="ao-composer-pad" style={{ flexShrink: 0, padding: "0 36px 26px" }}>
      <div style={{ maxWidth: chatMaxWidth, margin: "0 auto", transition: "max-width .25s" }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 13, flexWrap: "wrap" }}>
          {suggestions.map((sug, i) => (
            <button key={i} onClick={() => void sendText(sug)} className="ao-h-chip"
              style={{ padding: "7px 13px", borderRadius: 99, border: "1px solid var(--border-2)", background: "var(--surface)", color: "var(--text-3)", fontSize: 12.5, cursor: "pointer", whiteSpace: "nowrap" }}>{sug}</button>
          ))}
        </div>
        {/* P1-6: a message typed while a turn streams is queued VISIBLY, never silently lost. */}
        {queued && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 12px", marginBottom: 8, borderRadius: 10, border: "1px solid rgba(129,140,248,.3)", background: "rgba(99,102,241,.08)", fontSize: 12, color: "var(--text-2)" }}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}><circle cx="12" cy="12" r="9" stroke="var(--accent-2)" strokeWidth="2" /><path d="M12 7v5l3 3" stroke="var(--accent-2)" strokeWidth="2" strokeLinecap="round" /></svg>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              Queued — sends when the current turn finishes: “{queued}”
            </span>
          </div>
        )}
        <div style={{ border: "1px solid var(--border-2)", borderRadius: 16, background: "var(--surface-2)", padding: "14px 15px 11px" }}>
          <textarea value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendText(input); } }}
            rows={1} placeholder="Ask AegisOps to provision, investigate, deploy, or explain…"
            style={{ width: "100%", background: "transparent", border: "none", outline: "none", color: "var(--text)", fontSize: 15, lineHeight: 1.5, minHeight: 24, maxHeight: 160 }} />
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginTop: 9 }}>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-5)", fontFamily: "'IBM Plex Mono',monospace" }}>Approval required</span>
            <button onClick={() => void sendText(input)} disabled={!canSend} style={{ width: 35, height: 35, borderRadius: 9, border: "none", background: canSend ? "var(--accent)" : "var(--border-2)", display: "flex", alignItems: "center", justifyContent: "center", cursor: canSend ? "pointer" : "default" }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </button>
          </div>
        </div>
        <div style={{ textAlign: "center", fontSize: 11, color: "var(--text-5)", marginTop: 10 }}>
          {model} · AegisOps can make mistakes. Every destructive action requires your approval.
        </div>
      </div>
    </div>
  );
}

const avatarUser: React.CSSProperties = { width: 30, height: 30, borderRadius: 8, background: "var(--av-user-bg)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 600, color: "var(--av-user-fg)", flexShrink: 0 };
const avatarAI: React.CSSProperties = { width: 30, height: 30, borderRadius: 8, background: "linear-gradient(155deg,var(--accent),var(--accent-strong))", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 };

function Chip({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--text-3)", padding: "4px 10px", borderRadius: 7, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
      {label}<span style={{ color: "var(--text)", fontWeight: 500, fontFamily: mono ? "'IBM Plex Mono',monospace" : undefined }}>{value}</span>
    </span>
  );
}
function chipMono(bg: string, color: string): React.CSSProperties {
  return { fontFamily: "'IBM Plex Mono',monospace", fontSize: 11.5, padding: "3px 8px", borderRadius: 6, background: bg, color };
}
function fbBtn(border: string, bg: string, color: string): React.CSSProperties {
  return { width: 30, height: 30, borderRadius: 8, border: `1px solid ${border}`, background: bg, display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", color };
}
