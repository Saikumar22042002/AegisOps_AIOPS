# 03_TEST_MATRIX.md — the Phase-A automated suite (this is what ends the manual-bug loop)

Build this BEFORE fixing feature bugs. It runs against the REAL stack (containerized PG/Redis/Neo4j + live cloud creds, as PROGRESS describes) and encodes the safety invariants so regressions fail a test instead of surfacing in manual UI testing. Extend the existing 210 pytest / 20 vitest / 9 Playwright suites — don't replace them.

## Tiering (so a bare checkout still runs)
- **Unit / contract** — no cloud creds needed (routing, schemas, guards, redaction, memory plumbing, SSE contract). Must always run.
- **Integration** — real PG/Redis/Neo4j; skip cleanly if a datastore is absent.
- **Live-cloud** — real AWS/Azure/GCP; skip cleanly if creds absent; gated behind a flag; **destroy anything they create** in teardown.
- **E2E (Playwright)** — real login → UI journeys.

---

## A. SAFETY INVARIANTS (highest value — these catch N-08 and the whole destructive class)

**A1 — create never destroys.** For every resource type, on a create request:
- the generated plan contains **zero** `destroy` and zero `replace (-/+)` actions;
- after apply, any previously-existing resource **still exists** (query the cloud/inventory to confirm).

**A2 — two sequential creates coexist.** Create resource X, then create resource Y in the same session:
- both exist afterward; each has a **distinct Terraform workspace + state path**; applying Y did not touch X's state.

**A3 — destroy only removes the named target.** A destroy request:
- resolves exactly one target from inventory, requires confirmation + approval;
- the plan contains only `destroy` for that target; nothing else is destroyed;
- unrelated resources still exist afterward.

**A4 — action-vs-operation guard.** Given a classified action (create|read|modify|destroy), the Terraform operation about to run must match; a deliberately mismatched case is **blocked and surfaced**, never executed. (Test by forcing a mismatch.)

**A5 — read/status never mutates.** Every read/inventory/status phrasing (a big parametrized list) routes to a read-only intent and produces **no plan with any create/destroy/modify action**.

**A6 — no create↔destroy swap (both directions), parametrized over phrasings & clouds:**
- "create/provision/spin up/launch a new …" → create intent, create-only plan.
- "destroy/delete/terminate/tear down/remove …" → destroy intent, destroy-only plan.

---

## B. ROUTING MATRIX (parametrized)
For each cloud {aws, azure, gcp} × action {create, read, modify, destroy} × 3–5 phrasings each (canonical + synonyms + messy/natural):
- classifies to the correct intent + confidence logged;
- routes to the correct per-cloud module (no cross-cloud, no cross-action);
- ambiguous cloud → asks (never defaults to AWS);
- unsupported combo → honest clarification (never a wrong plan).

## C. PARAMETER COLLECTION & PROVIDER ACCURACY
For every real module:
- asks exactly its decision-critical params, defaults the rest;
- rejects genuinely-invalid values with a message listing valid options; **accepts all values the provider/sandbox actually supports** (Azure VM: Windows + Ubuntu/Debian + B/D/E-series; default RG) — guards N-05;
- no cross-cloud leakage (GCP `machine_type` never `ec2-*`, etc.).

## D. MEMORY & CONTINUITY (guards N-03)
- Ask 3 distinct things in one session, then "what have I asked you so far?" → response lists all 3.
- "my previous question" / "as I said earlier" → resolves to the real prior turn (not "beginning of our conversation").
- Long thread (> N turns) → early facts still recalled via summary; never "context window is blank."
- Resource reference ("the instance I just created") → resolves against inventory to the real resource.

## E. RUN LIFECYCLE (guards N-01)
- An apply run reaches **completed** with Verification **green**; ServiceNow closed, context-graph outcome written, timeline shows a real total.
- A deliberately unverifiable apply → **warned/failed** with a reason within the timeout; never a hung spinner.
- A destroy run reaches completed with a clean teardown.

## F. USABLE OUTPUTS (guards N-02, N-06)
- After a VM apply: SG/NSG/firewall contains the expected ingress for the collected CIDR; the success card includes host/user/port/key-download; key reveal returns a valid credential **exactly once** and never appears in logs/DB/graph.
- After S3/VPC/DB apply: the success card includes the real identifiers (name/ARN/region / id/CIDR / endpoint/secret-ref).

## G. SECURITY / REDACTION (regression guards)
- Free-text and structured redaction mask private keys, passwords, tokens, session tokens, access keys, ASIA ids, quoted-JSON secrets — across logs, streams, DB, and the context graph.
- Graceful provider errors (403/SERVICE_DISABLED/409/quota/expired-creds) produce a classified, friendly message + next step (guards the earlier BUG-05 wins; screenshots 8/9).

## H. RENDERING (guards N-04, N-07) — vitest/RTL
- An assistant message containing markdown renders **formatted** elements (bold/list/code/link/table), not raw `**`/`###`.
- The chat bubble does **not** duplicate the timeline Finalize text verbatim.
- Streaming renders progressively; code blocks have a copy control.

## I. E2E JOURNEYS (Playwright, real login)
- Continuous conversation: 3 turns + a "what did I ask?" recall turn.
- Create two VMs → both visible in inventory (create-never-destroys, end-to-end).
- Destroy one → only that one gone.
- Provision S3 → success card in chat.
- Theme + mobile smoke (existing).

---

## How to run / report
- `make test` runs unit+integration+vitest in-container (per PROGRESS 6.1); live-cloud + E2E behind flags with fresh creds.
- Hand-back report: total tests, pass/fail, coverage by section A–I, and which invariants (A1–A6) are proven live vs. plan-only. Any red is a bug to fix in Phase B, not a note to defer.

## Why this matters
Every defect you hit manually maps to a section here: create-deletes-previous → A1/A2; destroy-provisions → A3/A6; memory → D; verification hang → E; SSH dead / S3 silent → F; Azure OS → C; markdown → H. Once these are green, those bugs can't come back without failing a test — which is the difference between a demo and a production system.
```
