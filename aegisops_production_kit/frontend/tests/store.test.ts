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
      { event: "done", data: { runId: "run-abc", messageId: "msg-1" } },
    ]);

    await useUI.getState().sendText("provision an s3 bucket");

    const m = aiMsg();
    expect(m.runId).toBe("run-abc");                 // per-message run binding
    expect(m.steps?.map((s) => s.label)).toEqual(["Understood intent", "Routed → cloudops"]);
    expect(m.text).toBe("Hello world");              // token stream accumulated
    expect(m.showTimeline).toBe(false);              // switches to answer once tokens arrive
    expect(m.confidentiality).toEqual({ level: "High", score: 0.91 });  // badge data
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
