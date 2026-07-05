// RTL tests (6.1) — the Workspace renders real message state: confidentiality badge, feedback
// controls, the streaming cursor, the "Required to proceed" param card, and per-message artifact
// selection on click. State is driven through the store (no backend); auth/scroll are stubbed.

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Workspace } from "../components/Workspace";
import { AuthProvider } from "../lib/auth";
import { useUI } from "../lib/store";

beforeEach(() => {
  // AuthProvider probes /auth/me on mount → reject → unauthenticated (no approver affordances).
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
  // jsdom doesn't implement scrollIntoView (Conversation calls it in an effect).
  Element.prototype.scrollIntoView = vi.fn();
  useUI.setState({
    messages: [], streaming: false, selectedMessageId: null, artifactOpen: true,
    artifactNonce: 0, feedback: {}, approval: "pending",
  });
});

function renderWorkspace() {
  return render(
    <AuthProvider>
      <Workspace />
    </AuthProvider>,
  );
}

describe("Workspace message rendering", () => {
  it("renders a completed AI message with its confidentiality badge and feedback controls", async () => {
    useUI.setState({
      messages: [
        { id: "u1", isUser: true, text: "provision an s3 bucket" },
        { id: "ai1", isAI: true, text: "Provisioned the bucket.", done: true, streaming: false, runId: "run-abc12345",
          confidentiality: { level: "High", score: 0.9 }, references: [], steps: [], tab: "conversation" },
      ],
    });
    renderWorkspace();

    expect(await screen.findByText("Provisioned the bucket.")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();          // confidentiality badge
    expect(screen.getByText("Was this helpful?")).toBeInTheDocument();  // feedback controls (done)
    expect(screen.getByText(/ctx/)).toBeInTheDocument();           // run id chip
  });

  it("shows a streaming cursor and no feedback while the message is still streaming", async () => {
    useUI.setState({
      messages: [{ id: "ai2", isAI: true, text: "Working", streaming: true, done: false,
                   showTimeline: false, references: [], steps: [], tab: "conversation" }],
    });
    renderWorkspace();
    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(screen.queryByText("Was this helpful?")).not.toBeInTheDocument();
  });

  it("renders the 'Required to proceed' param card with each required field", async () => {
    useUI.setState({
      messages: [{ id: "ai3", isAI: true, text: "I need a few details.", streaming: false, done: false,
        references: [], steps: [], tab: "conversation",
        paramRequest: { template: "aws.ec2", items: [
          { name: "name", label: "Instance name", help: "e.g. web-01" },
          { name: "os", label: "Operating system", choices: ["ubuntu-22.04", "amazon-linux-2023"] },
        ] } }],
    });
    renderWorkspace();
    expect(await screen.findByText("Required to proceed")).toBeInTheDocument();
    expect(screen.getByText("Instance name")).toBeInTheDocument();
    expect(screen.getByText("Operating system")).toBeInTheDocument();
    expect(screen.getByText("ubuntu-22.04")).toBeInTheDocument();
    expect(screen.getAllByText("required").length).toBe(2);
  });

  it("clicking a message pins the artifact panel to that message's run", async () => {
    useUI.setState({
      messages: [
        { id: "ai-a", isAI: true, text: "answer A", done: true, runId: "rA", references: [], steps: [], tab: "conversation" },
        { id: "ai-b", isAI: true, text: "answer B", done: true, runId: "rB", references: [], steps: [], tab: "conversation" },
      ],
      selectedMessageId: "ai-a",
    });
    renderWorkspace();
    fireEvent.click(await screen.findByText("answer B"));
    expect(useUI.getState().selectedMessageId).toBe("ai-b");
    expect(useUI.getState().artifactOpen).toBe(true);
  });

  it("renders the empty state when there are no messages", async () => {
    renderWorkspace();
    expect(await screen.findByText("How can I help with your infrastructure?")).toBeInTheDocument();
  });
});
