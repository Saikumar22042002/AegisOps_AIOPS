# 01_STABILIZATION_REQUIREMENTS.md — production-grade acceptance spec

Acceptance spec for the test-first stabilization pass. A requirement is done only when it works against the REAL stack with live creds AND has an automated test guarding it (see `03_TEST_MATRIX.md`).

---

## 0. Guiding bar
AegisOps is a **production operations platform**, not a demo. Within a session it must feel as continuous and polished as ChatGPT/Claude while doing real, governed cloud work. Every provisioned resource must be **usable**; every run must **complete or clearly fail**; every message must be **clean and rendered**; the agent must **remember the conversation**; and a request must **only ever perform the action the user asked for** — never a destructive side effect.

## Method: tests first (this is what ends the manual-bug loop)
The core problem isn't any single bug — it's that manual UI testing only covers a few paths, so the same *classes* of defect keep reappearing. The fix is a comprehensive automated suite (Phase A) that encodes the **safety invariants** and the full cloud×action matrix, run against the real stack. Fix until green (Phase B), then do the parity work tests can't fully capture (Phase C), then hand back with the suite green (Phase D). Human testing becomes confirmation, not discovery.

---

## 1. Action & workflow safety (owner: Backend + Platform/Cloud) — HIGHEST PRIORITY, DESTRUCTIVE
This is the #1 requirement. Two confirmed misfires: a **create** request deleted an existing instance; a **destroy** request started provisioning.
- A **create/provision** request ONLY creates. It must never delete, replace, or modify an existing resource as a side effect.
- Each provisioned resource has its **own isolated Terraform workspace + state** so a subsequent `apply` cannot destroy/recreate a prior resource. (Shared/reused state is the prime suspect for "create deletes the previous instance.")
- A **destroy** request ONLY destroys the explicitly resolved + confirmed target; it never enters a create path.
- A **hard guard** at the workflow boundary compares the classified action (create|read|modify|destroy) to the actual Terraform operation; any mismatch is blocked and surfaced, not executed.
- Before the approval gate, every plan is inspected: a create plan containing any `destroy`/`replace` action halts with an explanation.
- Root-cause covers BOTH router misclassification AND state/workspace isolation.
- Tests (both directions, every resource type): create-never-destroys (zero destroy actions in plan; prior resource still exists after); destroy-never-creates; two sequential creates → two coexisting resources with distinct state; action-vs-operation guard blocks a mismatch.

## 2. Conversational memory & continuity (owner: AI Engineer) — CRITICAL
- Every LLM call for a session receives the **full prior transcript** (all user+assistant turns), not just the current message. (Screenshots 16/18 prove it currently doesn't.)
- Long threads: rolling **summary + recent-window** within a token budget; never "my context window is blank."
- "What was my previous question / what have I asked / what did you do" answered from real session history.
- Reference resolution spans BOTH: conversational ("the previous question") → transcript; resource ("the instance I just created") → inventory/context graph.
- Within-session continuity indistinguishable from ChatGPT/Claude; cross-session resource recall via inventory.
- Tests: ask 3 things → "what did I ask?" lists all 3; reference to a prior turn resolves; long-thread summarization keeps early facts.

## 3. Usable provisioned resources (owner: Platform/Cloud)
- **VM connectivity is real.** After a VM apply the user can actually connect: collect an allowed source CIDR (default closed, with a note); open the port for it in SG/NSG/firewall; deliver the credential usefully.
- **Credential delivery in-product:** private key / Windows password retrievable via a **secure one-time reveal/download in the UI**, never only via a CLI command; never logged/persisted in plaintext.
- "How do I connect without the key/password?" has a real in-product answer (reveal key, reset password, or session-manager access where available).
- Every successful provision posts a **resource-appropriate success card** in chat: VM→host/user/port/key-download/working connect command; S3→name/ARN/region/console URL; VPC→id/CIDR; DB→endpoint/secret-ref.
- Tests: post-apply the SG/NSG contains the expected ingress; the success card contains real identifiers; key reveal returns a valid key exactly once.

## 4. Runs complete or fail cleanly (owner: Backend)
- **Verification** must terminate: confirm outputs/health → node green → run `completed`; can't confirm within a timeout → warned/failed with reason. Never an infinite spinner. (Screenshots 4/5/19.)
- On terminal state: ServiceNow closed with outcome, context-graph outcome written, timeline shows real total duration, artifact panel stops "running."
- Tests: an apply run reaches `completed` with Verification green; a deliberately unverifiable run resolves to warned/failed, not hung.

## 5. Provider-accurate service modules (owner: Platform/Cloud)
- **Azure VM**: support Windows Server + Ubuntu/Debian images and the B/D/E-series sizes the subscription allows; use/create a default resource group like the portal; reject only genuinely invalid input. (Screenshots 6 vs 7.)
- Align OS/size/region choice lists for AWS/Azure/GCP with what each provider offers and the sandbox permits; validation messages list the valid options.
- **Destroy works for every supported resource** (ties to §1) — verify EC2/S3/VPC/RDS/EKS and Azure/GCP equivalents end-to-end.
- Tests: Azure VM accepts a Windows image + D-series size and plans with a default RG; each module's destroy plans+applies a clean teardown.

## 6. Rendering & product voice (owners: Frontend + AI Engineer)
- Assistant messages **render markdown** (bold, headings, lists, inline+fenced code with copy, links, tables) exactly like ChatGPT/Claude. No literal `**`/`###`/stray backticks. (Screenshots 8/9/11/15.)
- Content is concise, scannable; **no duplication** between the chat message and the timeline Finalize node (screenshots 15/16/18).
- Consistent senior-operator voice; success/failure/clarification use consistent card idioms; streaming renders smoothly; long outputs collapsible where sensible.
- Tests (RTL): a markdown message renders formatted elements, not raw syntax; finalize text is not duplicated verbatim in the bubble.

## 7. Robustness across the matrix (owner: Senior Reviewer)
Testing so far is partial, so the suite must proactively cover the SAME classes across the whole matrix, not just the exact screenshots:
- Every cloud × {create, read/inventory, modify, destroy} → correct routing, right params, plan→gate→apply/graceful-fail→verify→summarize→remember.
- No read/status/inventory phrasing routes to a destructive intent; no create routes to destroy; no destroy routes to create.
- Secrets never leak in logs/DB/graph/streams.
- Session memory holds across all of the above.

---

## Definition of done
- Phase-A suite exists, runs against the real stack, green.
- Requirements §1–§7 pass; N-01…N-08 fixed and each test-guarded.
- A user can: converse continuously with real memory; create two instances that both survive; destroy only the named resource; connect to a VM via a UI-delivered credential; see S3 details in chat; watch every run reach completed/clean-fail; create an Azure Windows VM; never see raw `**markdown**`.
- Existing suites stay green; PROGRESS.md updated (dated "Phase 8 — test-first stabilization"); hand-back includes a short list of genuinely subjective items for human spot-check.
```
