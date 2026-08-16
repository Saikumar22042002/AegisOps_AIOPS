// Store-logic tests (6.1) — the real message↔run binding, live streaming render, per-message
// artifact-panel selection, feedback, and history restore. The network edges (`sse`, `api`) are
// mocked so the store's event handling is exercised deterministically; every assertion is about
// the store's own behavior, not a stubbed value.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { streamSSE, type SSEEvent } from "../lib/sse";
import { useUI } from "../lib/store";

vi.mock("../lib/sse", () => ({ streamSSE: vi.fn() }));
vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn().mockResolvedValue({ sessions: [], projects: 0, incidents: 0, org: { name: "Acme" } }),
    post: vi.fn().mockResolvedValue({ id: "sess-1" }),
    patch: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue({}),
  },
}));

const mockStream = vi.mocked(streamSSE);
const mockApi = vi.mocked(api);

/** Replay a scripted SSE event list into the store's onEvent handler. */
function scriptStream(events: SSEEvent[]) {
  mockStream.mockImplementation(async (_path, _body, onEvent) => {
    for (const ev of events) onEvent(ev);
  });
}

const aiMsg = () => useUI.getState().messages.find((m) => m.isAI)!;

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.post.mockResolvedValue({ id: "sess-1" } as any);
  mockApi.get.mockResolvedValue({ sessions: [], projects: 0, incidents: 0, org: { name: "Acme" } } as any);
  useUI.setState({
    messages: [], streaming: false, activeRunId: null, selectedMessageId: null, sessionId: null,
    artifactNonce: 0, feedback: {}, approval: "pending", artifactOpen: true, activeArtifact: "timeline",
    runError: null, input: "",
  });
  window.localStorage.clear();
});

afterEach(() => useUI.setState({ messages: [], streaming: false }));

describe("sendText — live run binding + streaming render", () => {
  it("binds the assistant message to its run id from the first `run` event and renders the stream", async () => {
    scriptStream([
      { event: "run", data: { runId: "run-abc", sessionId: "sess-1" } },
      { event: "step", data: { index: 0, label: "Understood intent" } },
      { event: "step", data: { index: 1, label: "Routed → cloudops" } },
      { event: "token", data: { text: "Hello " } },
      { event: "token", data: { text: "world" } },
      { event: "confidentiality", data: { level: "High", score: 0.91 } },
      // P1.7: honest serving metadata (multi-provider substrate) — additive event.
      { event: "served_by", data: { provider: "google", model: "gemini-3.5-flash",
                                    requested_model: "gemini-3.5-flash", fallback_hop: 0 } },
      { event: "done", data: { runId: "run-abc", messageId: "msg-1" } },
    ]);

    await useUI.getState().sendText("provision an s3 bucket");

    const m = aiMsg();
    expect(m.runId).toBe("run-abc");                 // per-message run binding
    expect(m.steps?.map((s) => s.label)).toEqual(["Understood intent", "Routed → cloudops"]);
    expect(m.text).toBe("Hello world");              // token stream accumulated
    expect(m.showTimeline).toBe(false);              // switches to answer once tokens arrive
    expect(m.confidentiality).toEqual({ level: "High", score: 0.91 });  // badge data
    expect(m.servedBy).toEqual({ provider: "google", model: "gemini-3.5-flash",
                                 requestedModel: "gemini-3.5-flash", fallbackHop: 0 });
    expect(m.done).toBe(true);
    expect(m.messageId).toBe("msg-1");
    expect(m.streaming).toBe(false);
    expect(useUI.getState().activeRunId).toBe("run-abc");
    expect(useUI.getState().artifactNonce).toBeGreaterThan(0);  // panel refetched for this run
  });

  it("adopts the server session id and reuses it (no new session per message)", async () => {
    mockApi.post.mockResolvedValueOnce({ id: "sess-created" } as any);
    scriptStream([{ event: "run", data: { runId: "r1", sessionId: "sess-created" } },
                  { event: "done", data: { runId: "r1", messageId: "m1" } }]);
    await useUI.getState().sendText("hello");
    expect(useUI.getState().sessionId).toBe("sess-created");
  });

  it("an interrupt pins the panel to the run and opens the Terraform plan for approval", async () => {
    scriptStream([
      { event: "run", data: { runId: "run-int", sessionId: "sess-1" } },
      { event: "step", data: { label: "Awaiting approval" } },
      { event: "interrupt", data: { runId: "run-int", workflow: "aws.s3", plan: { summary: { add: 1, change: 0, destroy: 0 } } } },
    ]);

    await useUI.getState().sendText("create an s3 bucket named logs-prod");

    const m = aiMsg();
    const s = useUI.getState();
    expect(m.interrupt).toBeTruthy();
    expect(m.runId).toBe("run-int");
    expect(s.activeRunId).toBe("run-int");
    expect(s.selectedMessageId).toBe(m.id);          // artifact panel bound to THIS message
    expect(s.activeArtifact).toBe("terraform");
    expect(s.approval).toBe("pending");
    expect(s.artifactOpen).toBe(true);
  });

  it("surfaces a stream error on the message without crashing", async () => {
    scriptStream([
      { event: "run", data: { runId: "run-err" } },
      { event: "error", data: { message: "terraform plan failed: no creds", code: "terraform_error" } },
    ]);
    await useUI.getState().sendText("create a vm");
    expect(aiMsg().error).toContain("terraform plan failed");
    expect(useUI.getState().runError).toContain("terraform plan failed");
    expect(useUI.getState().streaming).toBe(false);
  });
});

describe("selectMessage — one artifact panel per message", () => {
  it("pins the panel to the clicked message and bumps the nonce; re-selecting is a no-op", () => {
    useUI.setState({
      messages: [
        { id: "m1", isAI: true, text: "a", runId: "rA" },
        { id: "m2", isAI: true, text: "b", runId: "rB" },
      ],
      selectedMessageId: "m1", artifactNonce: 5, artifactOpen: false,
    });
    useUI.getState().selectMessage("m2");
    expect(useUI.getState().selectedMessageId).toBe("m2");
    expect(useUI.getState().artifactOpen).toBe(true);
    expect(useUI.getState().artifactNonce).toBe(6);

    useUI.getState().selectMessage("m2");            // same message → no state churn
    expect(useUI.getState().artifactNonce).toBe(6);
  });
});

describe("submitFeedback — optimistic toggle", () => {
  it("sets then clears feedback and posts to the API", async () => {
    await useUI.getState().submitFeedback("msg-9", "up");
    expect(useUI.getState().feedback["msg-9"]).toBe("up");
    expect(mockApi.post).toHaveBeenCalledWith("/feedback", { messageId: "msg-9", value: "up" });

    await useUI.getState().submitFeedback("msg-9", "up");  // same value again → toggles off
    expect(useUI.getState().feedback["msg-9"]).toBeNull();
  });
});

describe("openSession — history restore keeps each message's own run", () => {
  it("maps every restored assistant message to its run and defaults the panel to the newest", async () => {
    mockApi.get.mockImplementation(async (path: string) => {
      if (path.includes("/messages")) {
        return {
          messages: [
            { id: "mu", role: "user", content: "hi" },
            { id: "ma1", role: "assistant", content: "first", run_id: "r1",
              confidentiality: { level: "Low", score: 0.1 }, analysis: {} },
            { id: "ma2", role: "assistant", content: "second", run_id: "r2",
              analysis: { reasoning: [], references: [] } },
          ],
        } as any;
      }
      return { sessions: [], projects: 0, incidents: 0, org: { name: "Acme" } } as any;
    });

    await useUI.getState().openSession("sess-x");

    const msgs = useUI.getState().messages;
    expect(msgs).toHaveLength(3);
    expect(msgs[1].runId).toBe("r1");                // each assistant message keeps its OWN run
    expect(msgs[2].runId).toBe("r2");
    expect(msgs[1].confidentiality).toEqual({ level: "Low", score: 0.1 });
    expect(useUI.getState().activeRunId).toBe("r2"); // panel defaults to the newest run
    expect(useUI.getState().selectedMessageId).toBe("ma2");
  });
});

describe("approveRun — P0-3 live apply UX (mid-apply progress, never a silent hang)", () => {
  const seedInterrupted = () =>
    useUI.setState({
      activeRunId: "r1", approval: "pending",
      messages: [
        { id: "u1", isUser: true, text: "create an s3 bucket" },
        { id: "ai1", isAI: true, text: "", runId: "r1", streaming: false, showTimeline: false,
          interrupt: { runId: "r1", workflow: "aws.s3" }, steps: [{ label: "Ran terraform plan" }],
          stepIdx: 0, consoleLines: [], references: [], tab: "conversation" },
      ],
    });

  it("flips the card instantly and re-enters the live streaming render BEFORE any event", async () => {
    seedInterrupted();
    let atStreamStart: any;
    mockStream.mockImplementation(async () => { atStreamStart = { ...aiMsg() }; });
    await useUI.getState().approveRun("approved");
    expect(atStreamStart.decision).toBe("approved");
    expect(atStreamStart.streaming).toBe(true);
    expect(atStreamStart.showTimeline).toBe(true);
  });

  it("streams apply steps + console live; done lands the success state", async () => {
    seedInterrupted();
    const n0 = useUI.getState().artifactNonce;
    scriptStream([
      { event: "step", data: { label: "Applying approved plan" } } as any,
      { event: "console", data: { stream: "stdout", line: "aws_s3_bucket.this: Creating..." } } as any,
      { event: "step", data: { label: "Verified live resource" } } as any,
      { event: "token", data: { text: "Created **bucket**." } } as any,
      { event: "done", data: { runId: "r1", messageId: "m1", outcome: {} } } as any,
    ]);
    await useUI.getState().approveRun("approved");
    const m = aiMsg();
    expect((m.steps ?? []).map((s) => s.label))
      .toEqual(["Ran terraform plan", "Applying approved plan", "Verified live resource"]);
    expect(m.consoleLines?.at(-1)?.line).toContain("Creating");
    expect(m.text).toContain("Created");
    expect(m.done).toBe(true);
    expect(m.streaming).toBe(false);
    // the docked timeline refetches per step, so it advances DURING the apply
    expect(useUI.getState().artifactNonce).toBeGreaterThanOrEqual(n0 + 2);
  });

  it("the spinner follows the NEWEST step mid-apply (stepIdx advances)", async () => {
    seedInterrupted();
    let midIdx = -1;
    mockStream.mockImplementation(async (_p, _b, onEvent) => {
      onEvent({ event: "step", data: { label: "Applying approved plan" } } as any);
      midIdx = aiMsg().stepIdx ?? -1;
      onEvent({ event: "done", data: { runId: "r1", messageId: "m1", outcome: {} } } as any);
    });
    await useUI.getState().approveRun("approved");
    expect(midIdx).toBe(1);
  });

  it("a rejection flips the card but never enters the applying render", async () => {
    seedInterrupted();
    let atStreamStart: any;
    mockStream.mockImplementation(async (_p, _b, onEvent) => {
      atStreamStart = { ...aiMsg() };
      onEvent({ event: "done", data: { runId: "r1", messageId: "m1", outcome: {} } } as any);
    });
    await useUI.getState().approveRun("rejected");
    expect(atStreamStart.decision).toBe("rejected");
    expect(atStreamStart.streaming).toBe(false);
    expect(aiMsg().done).toBe(true);
  });
});

describe("approveRun — P0-3 denial visibility + openSession card restoration", () => {
  const seedInterrupted = () =>
    useUI.setState({
      activeRunId: "r1", approval: "pending",
      messages: [
        { id: "ai1", isAI: true, text: "", runId: "r1", streaming: false, showTimeline: false,
          interrupt: { runId: "r1", workflow: "aws.s3" }, steps: [{ label: "plan" }], stepIdx: 0,
          consoleLines: [], references: [], tab: "conversation" },
      ],
    });

  it("a DENIED decision is loudly visible and the card comes back (the silent-403 bug)", async () => {
    seedInterrupted();
    mockStream.mockImplementation(async () => {
      throw new Error("Approval requires Cloud Architect, Org Admin, or Platform Admin.");
    });
    await useUI.getState().approveRun("approved");
    const m = aiMsg();
    expect(m.error).toContain("Approval requires");
    expect(m.decision).toBeNull();                       // the decision card returns
    expect(m.streaming).toBe(false);                     // never a phantom applying strip
    expect(useUI.getState().approval).toBe("pending");
    expect(useUI.getState().runError).toContain("Approval requires");
  });

  it("openSession rebuilds the approval card for a run still awaiting a decision", async () => {
    mockApi.get.mockImplementation(async (path: string) => {
      if (path.startsWith("/sessions/")) return { messages: [
        { id: "u1", role: "user", content: "create it" },
        { id: "a1", role: "assistant", content: "Drafted a plan.", run_id: "r9" },
      ] } as any;
      if (path === "/runs/r9") return { id: "r9", status: "awaiting_approval", workflow: "aws.s3",
                                        plan_json: { summary: { add: 4, change: 0, destroy: 0 } } } as any;
      return { sessions: [], projects: 0, incidents: 0, org: { name: "n" } } as any;
    });
    await useUI.getState().openSession("s1");
    const m = aiMsg();
    expect(m.interrupt).toMatchObject({ runId: "r9", workflow: "aws.s3" });
    expect((m.interrupt as any).plan.summary.add).toBe(4);
    expect(m.done).toBe(false);
  });

  it("a completed run restores as a plain transcript — no phantom card", async () => {
    mockApi.get.mockImplementation(async (path: string) => {
      if (path.startsWith("/sessions/")) return { messages: [
        { id: "a1", role: "assistant", content: "done", run_id: "r9" } ] } as any;
      if (path === "/runs/r9") return { id: "r9", status: "completed", workflow: "aws.s3",
                                        plan_json: {} } as any;
      return { sessions: [], projects: 0, incidents: 0, org: { name: "n" } } as any;
    });
    await useUI.getState().openSession("s1");
    expect(aiMsg().interrupt).toBeUndefined();
    expect(aiMsg().done).toBe(true);
  });
});

describe("sendText — P1-6 queue instead of silent drop", () => {
  it("a message sent mid-stream is queued visibly and auto-sends when the turn finishes", async () => {
    const calls: string[] = [];
    mockStream.mockImplementation(async (_p, body: any, onEvent) => {
      calls.push(body.message);
      if (calls.length === 1) {
        // mid-first-turn: the user types the follow-up — it must QUEUE, not vanish
        await useUI.getState().sendText("the follow-up");
        expect(useUI.getState().queued).toBe("the follow-up");
        expect(calls).toHaveLength(1);                    // no second POST yet
      }
      onEvent({ event: "done", data: { runId: `r${calls.length}`, messageId: "m", outcome: {} } } as any);
    });
    await useUI.getState().sendText("the first turn");
    expect(calls).toEqual(["the first turn", "the follow-up"]); // auto-sent on completion
    expect(useUI.getState().queued).toBeNull();
  });

  it("nothing queued → nothing extra sent", async () => {
    scriptStream([{ event: "done", data: { runId: "r1", messageId: "m", outcome: {} } } as any]);
    await useUI.getState().sendText("solo");
    expect(useUI.getState().queued).toBeNull();
  });
});
