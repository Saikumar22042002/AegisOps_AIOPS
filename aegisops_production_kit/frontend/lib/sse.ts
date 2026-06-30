// POST-based SSE client (EventSource only supports GET). Streams the API's chat/approval
// SSE response, parsing `event:`/`data:`/`id:` frames and invoking onEvent per event.

import { API_BASE } from "./api";

export interface SSEEvent {
  id?: string;
  event: string;
  data: Record<string, unknown>;
}

function parseFrame(raw: string): SSEEvent | null {
  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    else if (line.startsWith("id:")) id = line.slice(3).trim();
  }
  if (!dataLines.length) return null;
  try {
    return { id, event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { id, event, data: { raw: dataLines.join("\n") } };
  }
}

export async function streamSSE(
  path: string,
  body: unknown,
  onEvent: (ev: SSEEvent) => void,
  opts: { method?: string; signal?: AbortSignal } = {},
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: opts.method ?? "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: opts.signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON */
    }
    throw new Error(detail);
  }
  if (!res.body) throw new Error("No response stream");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // Normalize CRLF -> LF so frame splitting works regardless of the server's line
    // endings. sse-starlette emits `\r\n` line terminators (frames end with `\r\n\r\n`),
    // which a raw `indexOf("\n\n")` would never match — leaving the stream unparsed.
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const ev = parseFrame(frame);
      if (ev) onEvent(ev);
    }
  }
  // Flush a trailing frame with no terminating blank line (defensive).
  if (buffer.trim()) {
    const ev = parseFrame(buffer);
    if (ev) onEvent(ev);
  }
}
