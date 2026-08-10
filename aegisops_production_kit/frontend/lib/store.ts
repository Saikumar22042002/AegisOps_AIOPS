// UI store — mirrors the design's state model and drives the REAL backend.
// sendText streams from POST /chat (real graph SSE: step/token/analysis/reference/
// confidentiality/interrupt/done/error). Approvals call POST /approvals/{runId}; feedback
// calls POST /feedback. The pixel-exact components bind to this state unchanged.

import { create } from "zustand";

import { api } from "./api";
import { cloudToWire } from "./data";
import { streamSSE } from "./sse";
import type { ApprovalState, ArtifactTab, ChatMessage, MenuKey, NavKey, Overview, SessionMeta, ThemeMode } from "./types";

// Remember the open conversation so a page reload restores the same thread (ChatGPT-grade).
const LAST_SESSION_KEY = "aegisops.lastSession";
function persistLast(id: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (id) window.localStorage.setItem(LAST_SESSION_KEY, id);
    else window.localStorage.removeItem(LAST_SESSION_KEY);
  } catch {
    /* storage unavailable (private mode) — non-fatal */
  }
}
function readLast(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(LAST_SESSION_KEY);
  } catch {
    return null;
  }
}

// Map a persisted backend message into the UI ChatMessage shape used by the workspace.
function fromApiMessage(m: any): ChatMessage {
  if (m.role === "user") return { id: m.id, isUser: true, text: m.content ?? "" };
  const conf = m.confidentiality;
  const analysis = m.analysis || {};
  return {
    id: m.id,
    isAI: true,
    text: m.content ?? "",
    streaming: false,
    showTimeline: false,
    done: true,
    messageId: m.id,
    runId: m.run_id ?? undefined,
    confidentiality: conf && conf.level ? { level: conf.level, score: conf.score ?? 0 } : undefined,
    analysis: { summary: analysis.summary, cards: analysis.reasoning ?? [] },
    references: analysis.references ?? [],
    paramRequest: analysis.param_request ?? undefined,
    tab: "conversation",
  };
}

interface UIState {
  theme: ThemeMode;
  systemDark: boolean;
  artifactOpen: boolean;
  mobileNavOpen: boolean;
  activeArtifact: ArtifactTab;
  timelineOpen: boolean;
  approval: ApprovalState;
  activeNav: NavKey;
  input: string;
  messages: ChatMessage[];
  streaming: boolean;
  cmdkOpen: boolean;
  cmdkQuery: string;
  menu: MenuKey;
  org: string;
  env: string;
  cloud: string;
  region: string;
  model: string;
  role: string;
  feedback: Record<string, "up" | "down" | null>;
  activeRunId: string | null;
  // The message whose run the artifact panel is bound to. `null` = follow the latest run.
  // Clicking any past message pins the panel to THAT message's run (its own timeline/audit).
  selectedMessageId: string | null;
  sessionId: string | null;
  runError: string | null;
  // P1-6: a message typed while a turn streams — queued visibly, auto-sent on completion.
  queued: string | null;
  sessions: SessionMeta[];
  overview: Overview | null;
  artifactNonce: number;

  resolvedTheme: () => "dark" | "light";
  setSystemDark: (v: boolean) => void;
  cycleTheme: () => void;
  setTheme: (t: ThemeMode) => void;
  toggleMenu: (m: Exclude<MenuKey, null>) => void;
  closeMenus: () => void;
  toggleMobileNav: () => void;
  closeMobileNav: () => void;
  setSelector: (field: "org" | "env" | "cloud" | "region" | "model" | "role", value: string) => void;
  navTo: (nav: NavKey) => void;
  openArtifact: (tab: ArtifactTab) => void;
  selectMessage: (id: string) => void;
  toggleArtifact: () => void;
  closeArtifact: () => void;
  toggleTimeline: () => void;
  approveRun: (decision: "approved" | "rejected") => Promise<void>;
  setInput: (v: string) => void;
  sendText: (text: string) => Promise<void>;
  newChat: () => void;
  loadSidebar: () => Promise<void>;
  openSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  restoreLast: () => Promise<void>;
  /** GW-1: honor `?run=…` / `?session=…` / `&tab=…`; true when a link was consumed. */
  openDeepLink: () => Promise<boolean>;
  openCmdk: () => void;
  closeCmdk: () => void;
  setCmdkQuery: (v: string) => void;
  submitFeedback: (messageId: string, value: "up" | "down") => Promise<void>;
}

function patchMsg(set: any, id: string, patch: Partial<ChatMessage>) {
  set((s: UIState) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) }));
}

export const useUI = create<UIState>((set, get) => ({
  theme: "dark",
  systemDark: true,
  artifactOpen: true,
  mobileNavOpen: false,
  activeArtifact: "timeline",
  timelineOpen: false,
  approval: "pending",
  activeNav: "workspace",
  input: "",
  messages: [],
  streaming: false,
  cmdkOpen: false,
  cmdkQuery: "",
  menu: null,
  org: "Northwind Financial",
  env: "Production",
  cloud: "Auto (ask me)",  // U4: default to Auto so ambiguous requests ask which cloud
  region: "us-east-1",
  model: "gemini-3.5-flash",  // U3: a real, backend-served model id (menu = GET /models)
  role: "Platform Admin",
  feedback: {},
  activeRunId: null,
  selectedMessageId: null,
  sessionId: null,
  runError: null,
  queued: null,
  sessions: [],
  overview: null,
  artifactNonce: 0,

  resolvedTheme: () => {
    const s = get();
    return s.theme === "system" ? (s.systemDark ? "dark" : "light") : s.theme;
  },
  setSystemDark: (v) => set({ systemDark: v }),
  cycleTheme: () =>
    set((s) => ({ theme: s.theme === "dark" ? "light" : s.theme === "light" ? "system" : "dark" })),
  setTheme: (t) => set({ theme: t, menu: null }),
  toggleMenu: (m) => set((s) => ({ menu: s.menu === m ? null : m })),
  closeMenus: () => set({ menu: null }),
  toggleMobileNav: () => set((s) => ({ mobileNavOpen: !s.mobileNavOpen })),
  closeMobileNav: () => set({ mobileNavOpen: false }),
  setSelector: (field, value) => set({ [field]: value, menu: null } as Partial<UIState>),
  navTo: (nav) => set({ activeNav: nav, cmdkOpen: false, mobileNavOpen: false, menu: null }),
  openArtifact: (tab) => set({ artifactOpen: true, activeArtifact: tab }),
  // Pin the artifact panel to a specific message's run (its own timeline/reasoning/terraform/
  // logs/metrics/traces/references/approvals), fetched live from /runs/{runId}/*. Bumping the
  // nonce forces the panel to refetch for the newly selected run.
  selectMessage: (id) =>
    set((s) => (s.selectedMessageId === id ? {} : { selectedMessageId: id, artifactOpen: true, artifactNonce: s.artifactNonce + 1 })),
  toggleArtifact: () => set((s) => ({ artifactOpen: !s.artifactOpen })),
  closeArtifact: () => set({ artifactOpen: false }),
  toggleTimeline: () => set((s) => ({ timelineOpen: !s.timelineOpen })),
  setInput: (v) => set({ input: v }),
  openCmdk: () => set({ cmdkOpen: true }),
  closeCmdk: () => set({ cmdkOpen: false }),
  setCmdkQuery: (v) => set({ cmdkQuery: v }),

  newChat: () => {
    persistLast(null);
    set({ messages: [], input: "", activeNav: "workspace", streaming: false,
          activeRunId: null, selectedMessageId: null, sessionId: null, runError: null, approval: "pending" });
  },

  loadSidebar: async () => {
    const [sessions, overview] = await Promise.allSettled([
      api.get<{ sessions: SessionMeta[] }>("/sessions"),
      api.get<Overview>("/overview"),
    ]);
    if (sessions.status === "fulfilled") set({ sessions: sessions.value.sessions });
    if (overview.status === "fulfilled") set({ overview: overview.value });
  },

  openSession: async (id) => {
    set({ activeNav: "workspace", mobileNavOpen: false, sessionId: id, messages: [],
          streaming: false, runError: null, approval: "pending", activeRunId: null, selectedMessageId: null });
    persistLast(id);
    try {
      const { messages } = await api.get<{ messages: any[] }>(`/sessions/${id}/messages`);
      // Each restored message keeps its OWN run_id (see fromApiMessage) so clicking any of them
      // pins the panel to that message's run. Default the panel to the most recent run.
      const mapped = messages.map(fromApiMessage);
      const lastRunMsg = [...mapped].reverse().find((m) => m.isAI && m.runId);
      set({ messages: mapped, activeRunId: lastRunMsg?.runId ?? null,
            selectedMessageId: lastRunMsg?.id ?? null, artifactNonce: get().artifactNonce + 1 });
      // P0-3: rebuild the approval card for a run still awaiting a decision. The approver
      // may open the session from a different window/device than the one that streamed the
      // interrupt live (page reload, second device, another approver in the org) — without
      // this restore, approving from any freshly-opened session was impossible.
      if (lastRunMsg?.runId) {
        try {
          const run = await api.get<any>(`/runs/${lastRunMsg.runId}`);
          if (run.status === "awaiting_approval")
            patchMsg(set, lastRunMsg.id, {
              done: false,
              interrupt: { runId: run.id, workflow: run.workflow, plan: run.plan_json },
            });
        } catch {
          /* run unreadable (gone / cross-org 404) — keep the plain transcript */
        }
      }
    } catch (e) {
      set({ runError: e instanceof Error ? e.message : "failed to load conversation" });
    }
  },

  renameSession: async (id, title) => {
    const t = (title || "").trim();
    if (!t) return;
    set((s) => ({ sessions: s.sessions.map((x) => (x.id === id ? { ...x, title: t } : x)) }));
    try {
      await api.patch(`/sessions/${id}`, { title: t });
    } catch {
      void get().loadSidebar(); // revert to server truth on failure
    }
  },

  deleteSession: async (id) => {
    const wasActive = get().sessionId === id;
    set((s) => ({ sessions: s.sessions.filter((x) => x.id !== id) }));
    try {
      await api.del(`/sessions/${id}`);
    } catch {
      void get().loadSidebar();
      return;
    }
    if (wasActive) get().newChat();
    void get().loadSidebar();
  },

  restoreLast: async () => {
    // GW-1: a deep link wins over the remembered thread. When a chat channel truncates or
    // withholds an answer it sends `?run=<id>[&tab=…]`, and that link must actually land on
    // the run — otherwise "open it in AegisOps" is a promise the app doesn't keep.
    if (await get().openDeepLink()) return;
    await get().loadSidebar();
    const last = readLast();
    if (last && get().sessions.some((s) => s.id === last)) {
      await get().openSession(last);
    }
  },

  openDeepLink: async () => {
    if (typeof window === "undefined") return false;
    const params = new URLSearchParams(window.location.search);
    const runId = params.get("run");
    const sessionId = params.get("session");
    const tab = params.get("tab") as ArtifactTab | null;
    if (!runId && !sessionId) return false;

    await get().loadSidebar();
    let sid = sessionId;
    if (!sid && runId) {
      try {
        sid = (await api.get<{ session_id?: string | null }>(`/runs/${runId}`)).session_id ?? null;
      } catch {
        sid = null; // gone, or another org's run (404) — fall through to a clean workspace
      }
    }
    if (sid) await get().openSession(sid);
    if (runId) {
      set((s) => ({ activeNav: "workspace", activeRunId: runId, artifactOpen: true,
                    activeArtifact: tab ?? "timeline", artifactNonce: s.artifactNonce + 1 }));
    } else if (tab) {
      set({ artifactOpen: true, activeArtifact: tab });
    }
    // Drop the query so a refresh doesn't re-drive the link (and the URL stays shareable-clean).
    window.history.replaceState({}, "", window.location.pathname);
    return true;
  },

  sendText: async (text) => {
    const t = (text || "").trim();
    const s0 = get();
    if (!t) return;
    // STAB P1-6: typing Enter while a turn streams used to be a SILENT no-op (the text sat
    // in the box, the turn was lost — surfaced by the P0-2 retest harness). The message is
    // now QUEUED with visible feedback and auto-sends the moment the current turn finishes.
    if (s0.streaming) {
      set({ queued: t, input: "" });
      return;
    }
    const aiId = "ai" + Date.now();
    set((s) => ({
      input: "", streaming: true, runError: null,
      // Follow the new run live: pin the panel to this message so its timeline updates in real
      // time. Bump the nonce so the panel drops any previously-shown run immediately.
      selectedMessageId: aiId, artifactNonce: s.artifactNonce + 1,
      messages: [
        ...s.messages,
        { id: "u" + Date.now(), isUser: true, text: t },
        { id: aiId, isAI: true, text: "", streaming: true, showTimeline: true, steps: [], stepIdx: 0,
          references: [], consoleLines: [], tab: "conversation" },
      ],
    }));

    // Reuse the thread's session across messages; create one on the first message so the
    // backend persists every message under a single conversation (not a new one each time).
    let sid = s0.sessionId;
    if (!sid) {
      try {
        const created = await api.post<{ id: string }>("/sessions", { title: t.slice(0, 80) || "New conversation" });
        sid = created.id;
        set({ sessionId: sid });
        persistLast(sid);
      } catch {
        /* fall back to backend auto-create (sid stays null); reuse won't persist until reload */
      }
    }

    // U4: "Auto (ask me)" → cloud=null on the wire, so the backend never uses the selector as a
    // hint and an ambiguous request triggers the clarifying question.
    const ctx = { org: s0.org, env: s0.env, cloud: cloudToWire(s0.cloud), region: s0.region, role: s0.role };
    try {
      await streamSSE("/chat", { sessionId: sid, message: t, model: s0.model, context: ctx }, (ev) => {
        const m = get().messages.find((x) => x.id === aiId);
        if (!m) return;
        switch (ev.event) {
          case "run": {
            // First event: bind this message to its real run id (panel goes live, message links
            // to its run from the start). Adopt the server session id if we have none yet.
            const rid = String(ev.data.runId);
            patchMsg(set, aiId, { runId: rid });
            set((s) => ({ activeRunId: rid,
                          sessionId: s.sessionId ?? (ev.data.sessionId ? String(ev.data.sessionId) : null) }));
            break;
          }
          case "step": {
            const steps = [...(m.steps ?? []), { label: String(ev.data.label) }];
            patchMsg(set, aiId, { steps, stepIdx: steps.length - 1 });
            break;
          }
          case "token":
            patchMsg(set, aiId, { showTimeline: false, text: (m.text ?? "") + String(ev.data.text ?? "") });
            break;
          case "analysis":
            patchMsg(set, aiId, { analysis: { summary: String(ev.data.summary ?? ""), cards: (ev.data.reasoningCards as any) ?? [] } });
            break;
          case "params":
            // Structured "required inputs" request → render the param card on this message.
            patchMsg(set, aiId, { paramRequest: ev.data as any });
            break;
          case "reference":
            patchMsg(set, aiId, { references: [...(m.references ?? []), ev.data as any] });
            break;
          case "confidentiality":
            patchMsg(set, aiId, { confidentiality: { level: String(ev.data.level), score: Number(ev.data.score) } });
            break;
          case "console":
            patchMsg(set, aiId, { consoleLines: [...(m.consoleLines ?? []), { stream: String(ev.data.stream), line: String(ev.data.line) }] });
            break;
          case "interrupt":
            patchMsg(set, aiId, { interrupt: ev.data, runId: String(ev.data.runId), showTimeline: false });
            // Approval needs attention — pull the panel to this run and show its plan.
            set((s) => ({ activeRunId: String(ev.data.runId), approval: "pending", artifactOpen: true,
                          activeArtifact: "terraform", selectedMessageId: aiId, artifactNonce: s.artifactNonce + 1 }));
            break;
          case "error":
            // U7: `retry` carries a one-click retry-with-fix ({label, retry_message}) — the
            // button re-sends the corrected message as a genuine new turn.
            patchMsg(set, aiId, { error: String(ev.data.message), streaming: false,
                                  retry: (ev.data as any).retry ?? undefined });
            set({ runError: String(ev.data.message) });
            break;
          case "done": {
            const sens = ((ev.data.outcome as any)?.sensitive_outputs as string[]) ?? [];
            patchMsg(set, aiId, { streaming: false, done: true, runId: String(ev.data.runId),
                                  messageId: String(ev.data.messageId),
                                  sensitiveOutputs: sens.length ? sens : undefined });
            set((s) => ({ activeRunId: String(ev.data.runId), artifactNonce: s.artifactNonce + 1 }));
            break;
          }
        }
      });
    } catch (e) {
      patchMsg(set, aiId, { error: e instanceof Error ? e.message : "stream failed", streaming: false });
    } finally {
      set({ streaming: false });
      const m = get().messages.find((x) => x.id === aiId);
      if (m && !m.interrupt) patchMsg(set, aiId, { streaming: false });
      // Refresh the sidebar so the new/updated session (real title) and badge counts appear.
      void get().loadSidebar();
      // P1-6: a message queued mid-stream sends now, as its own real turn.
      const q = get().queued;
      if (q) {
        set({ queued: null });
        void get().sendText(q);
      }
    }
  },

  approveRun: async (decision) => {
    const runId = get().activeRunId;
    if (!runId) return;
    set((s) => ({ approval: decision, streaming: decision === "approved",
                  activeArtifact: "timeline", artifactNonce: s.artifactNonce + 1 }));
    const ai = get().messages.find((m) => m.runId === runId && m.isAI);
    const aiId = ai?.id;
    // P0-3: the card flips INSTANTLY (decision on the message) and an approved apply puts
    // the message back into the live streaming render — expanded activity + spinner on the
    // current step — exactly like a sendText run. Before this, minutes of terraform apply
    // rendered nothing in the conversation and read as hung.
    if (aiId) patchMsg(set, aiId, { decision, done: false,
                                    streaming: decision === "approved",
                                    showTimeline: decision === "approved" });
    try {
      await streamSSE(`/approvals/${runId}`, { decision }, (ev) => {
        if (!aiId) return;
        const m = get().messages.find((x) => x.id === aiId);
        if (!m) return;
        if (ev.event === "console")
          patchMsg(set, aiId, { consoleLines: [...(m.consoleLines ?? []), { stream: String(ev.data.stream), line: String(ev.data.line) }] });
        else if (ev.event === "token")
          patchMsg(set, aiId, { text: (m.text ?? "") + String(ev.data.text ?? "") });
        else if (ev.event === "step") {
          // stepIdx follows the newest step so its spinner is live (P0-3); the timeline
          // artifact refetches per step so the docked panel advances with the apply.
          const steps = [...(m.steps ?? []), { label: String(ev.data.label) }];
          patchMsg(set, aiId, { steps, stepIdx: steps.length - 1 });
          set((s) => ({ artifactNonce: s.artifactNonce + 1 }));
        }
        else if (ev.event === "done") {
          // Terminal: clear the MESSAGE's streaming flag so the artifact panel hands off from
          // the live spinner to the persisted timeline (N-01 — the "Verification" hang was this
          // flag never clearing on approval continuations), and surface revealable credentials.
          const sens = ((ev.data.outcome as any)?.sensitive_outputs as string[]) ?? [];
          patchMsg(set, aiId, { streaming: false, done: true, showTimeline: false,
                                sensitiveOutputs: sens.length ? sens : undefined });
        }
        else if (ev.event === "error")
          set({ runError: String(ev.data.message) });
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : "approval failed";
      // P0-3: a DENIED decision must be loudly visible — a 4xx on the approval call used to
      // render as pure silence (the "minutes of zero feedback"). The error lands on the
      // message and the card comes back so a legitimate approver can still act.
      set({ runError: msg, approval: "pending" });
      if (aiId) patchMsg(set, aiId, { decision: null, streaming: false, done: true, error: msg });
    } finally {
      // Defensive: even if `done` was missed (disconnect), never leave the live spinner up.
      if (aiId) patchMsg(set, aiId, { streaming: false });
      // Refetch the artifact tabs (timeline/logs/approvals) and badges for the resolved run.
      set((s) => ({ streaming: false, artifactNonce: s.artifactNonce + 1 }));
      void get().loadSidebar();
      // P1-6: a message typed during the apply sends now, as its own real turn.
      const q = get().queued;
      if (q) {
        set({ queued: null });
        void get().sendText(q);
      }
    }
  },

  submitFeedback: async (messageId, value) => {
    set((s) => ({ feedback: { ...s.feedback, [messageId]: s.feedback[messageId] === value ? null : value } }));
    try {
      await api.post("/feedback", { messageId, value });
    } catch {
      /* optimistic; ignore network errors for UX */
    }
  },
}));

export const useResolvedTheme = () =>
  useUI((s) => (s.theme === "system" ? (s.systemDark ? "dark" : "light") : s.theme));
