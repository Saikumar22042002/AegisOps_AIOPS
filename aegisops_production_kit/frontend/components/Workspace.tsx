"use client";

import { useEffect, useRef, useState } from "react";

import { useAuth } from "../lib/auth";
import { BrandShield } from "../lib/icons";
import { useUI } from "../lib/store";
import type { ChatMessage, ParamRequest } from "../lib/types";

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
        {(m.intent || m.workflow || conf) && (
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 14 }}>
            {m.intent && <Chip label="Intent" value={m.intent} />}
            {m.workflow && <Chip label="Workflow" value={m.workflow} mono />}
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

            <div style={{ fontSize: 15, color: "var(--text-2)", lineHeight: 1.78 }}>
              {m.text}
              {m.streaming && <span style={{ display: "inline-block", width: 7, height: 16, background: "var(--accent-2)", marginLeft: 2, verticalAlign: -2, animation: "ao-blink 1s steps(1) infinite" }} />}
            </div>

            {m.error && (
              <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--red-2)", background: "rgba(248,113,113,.08)", border: "1px solid rgba(248,113,113,.25)", borderRadius: 10, padding: "10px 13px" }}>{m.error}</div>
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
                    <div style={{ fontSize: 12.5, color: "var(--text-3)" }}>{String(m.interrupt?.workflow ?? "")} · approval required</div>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                    <span style={chipMono("rgba(52,211,153,.12)", "var(--green)")}>+{summary.add}</span>
                    <span style={chipMono("rgba(251,191,36,.12)", "var(--amber)")}>~{summary.change}</span>
                    <span style={chipMono("var(--surface-3)", "var(--text-4)")}>-{summary.destroy}</span>
                  </div>
                </button>

                {approval === "pending" && (
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
  const setInput = useUI((s) => s.setInput);
  const sendText = useUI((s) => s.sendText);
  const streaming = useUI((s) => s.streaming);
  const model = useUI((s) => s.model);
  const canSend = input.trim() && !streaming;
  const suggestions = [
    "Provision an S3 bucket in AWS us-east-1",
    "Why did checkout latency spike after the 14:20 deploy?",
    "Create a GCS bucket in my GCP project",
  ];
  return (
    <div className="ao-composer-pad" style={{ flexShrink: 0, padding: "0 36px 26px" }}>
      <div style={{ maxWidth: chatMaxWidth, margin: "0 auto", transition: "max-width .25s" }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 13, flexWrap: "wrap" }}>
          {suggestions.map((sug, i) => (
            <button key={i} onClick={() => void sendText(sug)} className="ao-h-chip"
              style={{ padding: "7px 13px", borderRadius: 99, border: "1px solid var(--border-2)", background: "var(--surface)", color: "var(--text-3)", fontSize: 12.5, cursor: "pointer", whiteSpace: "nowrap" }}>{sug}</button>
          ))}
        </div>
        <div style={{ border: "1px solid var(--border-2)", borderRadius: 16, background: "var(--surface-2)", padding: "14px 15px 11px" }}>
          <textarea value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendText(input); } }}
            rows={1} placeholder="Ask AegisOps to provision, investigate, deploy, or explain…"
            style={{ width: "100%", background: "transparent", border: "none", outline: "none", color: "var(--text)", fontSize: 15, lineHeight: 1.5, minHeight: 24, maxHeight: 160 }} />
          <div style={{ display: "flex", alignItems: "center", gap: 9, marginTop: 9 }}>
            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-5)", fontFamily: "'IBM Plex Mono',monospace" }}>Approval required</span>
            <button onClick={() => void sendText(input)} style={{ width: 35, height: 35, borderRadius: 9, border: "none", background: canSend ? "var(--accent)" : "var(--border-2)", display: "flex", alignItems: "center", justifyContent: "center", cursor: canSend ? "pointer" : "default" }}>
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
