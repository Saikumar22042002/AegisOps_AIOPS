// Phase-A RENDERING (03_TEST_MATRIX §H — guards N-04/N-07).
// Screenshots 8/9/11/15 show literal `**bold**` / `###` / backticks in the chat bubble.
// Assistant messages must render full markdown like ChatGPT/Claude — no raw syntax.

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Workspace } from "../components/Workspace";
import { AuthProvider } from "../lib/auth";
import { useUI } from "../lib/store";

const MD_MESSAGE = [
  "**The apply failed — The Azure service principal doesn't have permission.**",
  "",
  "### Next step",
  "- Grant the service principal the **Contributor** role",
  "- Then retry: `terraform apply`",
  "",
  "```bash",
  "az role assignment create --assignee <sp-id> --role Contributor",
  "```",
  "",
  "[Azure IAM docs](https://learn.microsoft.com/azure/role-based-access-control/)",
].join("\n");

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
  Element.prototype.scrollIntoView = vi.fn();
  useUI.setState({
    messages: [{ id: "md1", isAI: true, text: MD_MESSAGE, done: true, streaming: false,
                 references: [], steps: [], tab: "conversation" }],
    streaming: false, selectedMessageId: null, artifactOpen: true, artifactNonce: 0,
    feedback: {}, approval: "pending",
  });
});

function renderWorkspace() {
  return render(
    <AuthProvider>
      <Workspace />
    </AuthProvider>,
  );
}

describe("assistant markdown rendering (N-04)", () => {
  it("renders bold as <strong>, never literal ** markers", async () => {
    const { container } = renderWorkspace();
    expect(await screen.findAllByText(/Contributor/)).toBeTruthy();
    expect(container.textContent).not.toContain("**");            // no raw markers anywhere
    const strongs = container.querySelectorAll("strong");
    expect(strongs.length).toBeGreaterThan(0);
  });

  it("renders headings and lists as elements, not raw ### / -", async () => {
    const { container } = renderWorkspace();
    await screen.findAllByText(/Next step/);
    expect(container.textContent).not.toContain("###");
    expect(container.querySelectorAll("ul li").length).toBeGreaterThanOrEqual(2);
  });

  it("renders fenced code as a code block with a copy control", async () => {
    const { container } = renderWorkspace();
    await screen.findAllByText(/az role assignment create/);
    expect(container.querySelector("pre code, pre")).toBeTruthy();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  it("renders links as anchors", async () => {
    const { container } = renderWorkspace();
    const a = container.querySelector('a[href*="learn.microsoft.com"]');
    expect(a).toBeTruthy();
  });

  it("user messages stay plain text (no markdown injection surface)", async () => {
    useUI.setState({
      messages: [{ id: "u1", isUser: true, text: "**not bold** <b>nope</b>" }],
    });
    const { container } = renderWorkspace();
    expect(container.textContent).toContain("**not bold**");      // rendered literally
    expect(container.querySelector("b")).toBeNull();
  });
});
