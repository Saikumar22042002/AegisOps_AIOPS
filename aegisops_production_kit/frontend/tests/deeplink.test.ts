// GW-1 — the `?run=` / `?session=` deep link the chat gateway sends.
//
// This link is load-bearing, not cosmetic: when a channel WITHHOLDS a High-confidentiality
// answer or truncates a long one, the link is the only way the user reaches the real content.
// So it must actually land on the run — these tests pin that, plus the two edges that make it
// safe to hand out: a link for a run the caller cannot read must degrade to a clean workspace,
// and the query must not survive into a refresh.

import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { useUI } from "../lib/store";

vi.mock("../lib/sse", () => ({ streamSSE: vi.fn() }));
vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue({}),
  },
}));

const mockApi = vi.mocked(api);

function setUrl(search: string) {
  window.history.replaceState({}, "", `/${search}`);
}

/** Route the store's GETs: sidebar, the run read, and the session transcript. */
function routeApi(opts: { runSession?: string | null; runThrows?: boolean } = {}) {
  mockApi.get.mockImplementation(async (path: string) => {
    if (path === "/sessions") return { sessions: [{ id: "sess-9", title: "t", status: "active", created_at: "" }] } as any;
    if (path === "/overview") return { projects: 0, incidents: 0, org: { name: "Acme" } } as any;
    if (path.startsWith("/runs/")) {
      if (opts.runThrows) throw new Error("run not found");
      return { id: "run-1", session_id: opts.runSession ?? "sess-9", status: "completed" } as any;
    }
    if (path.startsWith("/sessions/")) return { messages: [] } as any;
    return {} as any;
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  useUI.setState({
    messages: [], streaming: false, activeRunId: null, selectedMessageId: null, sessionId: null,
    artifactNonce: 0, artifactOpen: false, activeArtifact: "timeline", runError: null,
    activeNav: "admin",
  });
  window.localStorage.clear();
  setUrl("");
});

describe("GW-1 deep link", () => {
  it("does nothing when there is no link", async () => {
    routeApi();
    expect(await useUI.getState().openDeepLink()).toBe(false);
    expect(useUI.getState().sessionId).toBeNull();
  });

  it("resolves ?run= to its session and pins the artifact panel to that run", async () => {
    routeApi({ runSession: "sess-9" });
    setUrl("?run=run-1&tab=terraform");

    expect(await useUI.getState().openDeepLink()).toBe(true);
    const s = useUI.getState();
    expect(s.sessionId).toBe("sess-9");        // the conversation is open…
    expect(s.activeRunId).toBe("run-1");       // …pinned to the run from the link
    expect(s.activeArtifact).toBe("terraform"); // …on the tab the link asked for
    expect(s.artifactOpen).toBe(true);
    expect(s.activeNav).toBe("workspace");     // navigated away from wherever we were
  });

  it("defaults to the timeline tab when the link names none", async () => {
    routeApi({ runSession: "sess-9" });
    setUrl("?run=run-1");
    await useUI.getState().openDeepLink();
    expect(useUI.getState().activeArtifact).toBe("timeline");
  });

  it("accepts a bare ?session= link", async () => {
    routeApi();
    setUrl("?session=sess-9");
    expect(await useUI.getState().openDeepLink()).toBe(true);
    expect(useUI.getState().sessionId).toBe("sess-9");
    // No run in the link ⇒ nothing is pinned.
    expect(useUI.getState().activeRunId).toBeNull();
  });

  it("degrades cleanly when the run is unreadable (gone, or another org's 404)", async () => {
    routeApi({ runThrows: true });
    setUrl("?run=run-1");
    // Consumed (so we don't then restore an unrelated thread), but no session opened.
    expect(await useUI.getState().openDeepLink()).toBe(true);
    expect(useUI.getState().sessionId).toBeNull();
    expect(useUI.getState().activeRunId).toBe("run-1");
  });

  it("clears the query so a refresh does not re-drive the link", async () => {
    routeApi({ runSession: "sess-9" });
    setUrl("?run=run-1&tab=logs");
    await useUI.getState().openDeepLink();
    expect(window.location.search).toBe("");
  });

  it("takes precedence over the remembered thread on boot", async () => {
    routeApi({ runSession: "sess-9" });
    window.localStorage.setItem("aegisops.lastSession", "some-other-session");
    setUrl("?run=run-1");

    await useUI.getState().restoreLast();
    // The link won: the remembered thread was never opened.
    expect(useUI.getState().sessionId).toBe("sess-9");
  });

  it("falls back to the remembered thread when there is no link", async () => {
    routeApi();
    setUrl("");
    await useUI.getState().restoreLast();
    // No link ⇒ normal restore path (nothing remembered here, so nothing opened).
    expect(useUI.getState().activeRunId).toBeNull();
  });
});
