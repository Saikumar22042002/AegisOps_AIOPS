// UI store — mirrors the design's state model and drives the REAL backend.
// sendText streams from POST /chat (real graph SSE: step/token/analysis/reference/
// confidentiality/interrupt/done/error). Approvals call POST /approvals/{runId}; feedback
// calls POST /feedback. The pixel-exact components bind to this state unchanged.

import { create } from "zustand";

import { api } from "./api";
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
  cloud: "AWS",
  region: "us-east-1",
  model: "Gemini 2.5 Pro",
  role: "Platform Admin",
  feedback: {},
  activeRunId: null,
  selectedMessageId: null,
  sessionId: null,
  runError: null,
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
    await get().loadSidebar();
    const last = readLast();
    if (last && get().sessions.some((s) => s.id === last)) {
      await get().openSession(last);
    }
  },

  sendText: async (text) => {
    const t = (text || "").trim();
    const s0 = get();
    if (!t || s0.streaming) return;
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

    const ctx = { org: s0.org, env: s0.env, cloud: s0.cloud, region: s0.region, role: s0.role };
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
            patchMsg(set, aiId, { error: String(ev.data.message), streaming: false });
            set({ runError: String(ev.data.message) });
            break;
          case "done":
            patchMsg(set, aiId, { streaming: false, done: true, runId: String(ev.data.runId),
                                  messageId: String(ev.data.messageId) });
            set((s) => ({ activeRunId: String(ev.data.runId), artifactNonce: s.artifactNonce + 1 }));
            break;
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
    }
  },

  approveRun: async (decision) => {
    const runId = get().activeRunId;
    if (!runId) return;
    set((s) => ({ approval: decision, streaming: decision === "approved",
                  activeArtifact: "timeline", artifactNonce: s.artifactNonce + 1 }));
    const ai = get().messages.find((m) => m.runId === runId && m.isAI);
    const aiId = ai?.id;
    try {
      await streamSSE(`/approvals/${runId}`, { decision }, (ev) => {
        if (!aiId) return;
        const m = get().messages.find((x) => x.id === aiId);
        if (!m) return;
        if (ev.event === "console")
          patchMsg(set, aiId, { consoleLines: [...(m.consoleLines ?? []), { stream: String(ev.data.stream), line: String(ev.data.line) }] });
        else if (ev.event === "token")
          patchMsg(set, aiId, { text: (m.text ?? "") + String(ev.data.text ?? "") });
        else if (ev.event === "step")
          patchMsg(set, aiId, { steps: [...(m.steps ?? []), { label: String(ev.data.label) }] });
        else if (ev.event === "error")
          set({ runError: String(ev.data.message) });
      });
    } catch (e) {
      set({ runError: e instanceof Error ? e.message : "approval failed" });
    } finally {
      // Refetch the artifact tabs (timeline/logs/approvals) and badges for the resolved run.
      set((s) => ({ streaming: false, artifactNonce: s.artifactNonce + 1 }));
      void get().loadSidebar();
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
