// SSE client parsing (6.1 / regression guard) — sse-starlette terminates frames with CRLF
// (`\r\n\r\n`). The client must normalize CRLF→LF before splitting on the blank line, or no
// event ever fires (the original "dead UI" bug). Also guards the trailing-frame flush.

import { describe, expect, it, vi } from "vitest";

import { streamSSE, type SSEEvent } from "../lib/sse";

function fetchReturning(chunks: string[]) {
  return vi.fn(async () => ({
    ok: true,
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        const enc = new TextEncoder();
        for (const c of chunks) controller.enqueue(enc.encode(c));
        controller.close();
      },
    }),
  }));
}

async function collect(chunks: string[]): Promise<SSEEvent[]> {
  vi.stubGlobal("fetch", fetchReturning(chunks));
  const events: SSEEvent[] = [];
  await streamSSE("/chat", { message: "x" }, (ev) => events.push(ev));
  return events;
}

describe("streamSSE frame parsing", () => {
  it("parses CRLF-terminated SSE frames (sse-starlette format)", async () => {
    const events = await collect([
      'event: run\r\ndata: {"runId":"r1","sessionId":"s1"}\r\nid: 1\r\n\r\n',
      'event: step\r\ndata: {"label":"Ran terraform plan"}\r\nid: 2\r\n\r\n',
    ]);
    expect(events.map((e) => e.event)).toEqual(["run", "step"]);
    expect(events[0].data).toEqual({ runId: "r1", sessionId: "s1" });
    expect(events[0].id).toBe("1");
    expect(events[1].data).toEqual({ label: "Ran terraform plan" });
  });

  it("reassembles events split across network chunks", async () => {
    const events = await collect(['event: token\r\nda', 'ta: {"text":"hi"}\r\n\r\n']);
    expect(events).toHaveLength(1);
    expect(events[0].data).toEqual({ text: "hi" });
  });

  it("flushes a trailing frame with no terminating blank line", async () => {
    const events = await collect(['event: done\r\ndata: {"ok":true}']);
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe("done");
  });

  it("throws with the API error detail on a non-ok response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false, statusText: "Forbidden",
      json: async () => ({ detail: "Approval requires an approver role." }),
    })));
    await expect(streamSSE("/approvals/x", { decision: "approved" }, () => {}))
      .rejects.toThrow("Approval requires an approver role.");
  });
});
