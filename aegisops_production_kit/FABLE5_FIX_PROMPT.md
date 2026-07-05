# FABLE5_FIX_PROMPT.md — paste into Claude Code (Fable 5) as your first message

> Setup before pasting: (1) copy the 20 screenshots into the repo at `Screenshots/1.png … 20.png`; (2) your existing `PROGRESS.md` is already at the repo root — keep it; (3) paste the block below.

---

You are continuing work on **AegisOps**, a ChatOps-native CloudOps platform (Next.js + FastAPI + LangGraph + Gemini + Terraform + Postgres/Redis/Neo4j). This is a **mature, working codebase**, not a fresh build.

## Context: who built this and where it stands
A previous model (**Claude Opus 4.8**) built the platform through **six milestones (M1–M6) plus Phases 1–6**, all documented in `PROGRESS.md` at the repo root. Read `PROGRESS.md` in full before doing anything — it is the authoritative record. Highlights so it's clear how far along this is:
- Real OIDC auth + RBAC, pixel-matched UI, SSE streaming chat on Gemini, LangGraph orchestrator, human-approval gates, Terraform apply, 8-tab artifact panel, Neo4j context graph, ServiceNow, Langfuse/OTel.
- Multi-cloud routing (Phase 2), cloud-agnostic param collection + usable SSH keys (Phase 3), resource inventory + day-2 ops (Phase 4), a 14-module catalog (Phase 5), and a real test suite — **210 backend pytest, 20 vitest, 9 Playwright** (Phase 6).
- Live-verified lifecycles: **AWS EC2** and **GCP Compute** full create→apply→day-2→destroy; AWS S3 apply; other clouds/services plan-verified.

So Opus 4.8 did substantial, verified work. **Do not rebuild or regress it.** Your job is a **targeted fix pass** on issues that a fresh round of manual UI testing surfaced (evidence in `Screenshots/1.png … 20.png`), then close the remaining gaps to reach 100% working.

## How to use the screenshots
The 20 screenshots are **real captures of the running app** during manual testing. `Screenshots/SCREENSHOT_INDEX.md` maps each image to what it shows and which issue it evidences. **Open the referenced screenshot before fixing each issue** so you see the exact UI/log/timeline state. I reference them by number below.

## CRITICAL framing — separate real bugs from provider-side failures
Several "errors" in the screenshots are **NOT code bugs** — they are cloud providers correctly rejecting a request, and the app correctly surfacing it. Your PROGRESS.md already anticipated some of these (Azure apply-deferred pending an SP with Contributor). Do **not** try to force these to succeed:
- **Azure `403 AuthorizationFailed`** (screenshot 9) — the Azure service principal lacks Contributor on the subscription. Known/expected per PROGRESS Phase 5. Infra fix, not code.
- **GCP `403 SERVICE_DISABLED`** (screenshot 12) — Compute Engine API not enabled on that project. Infra fix, not code.
- **S3 `409 BucketAlreadyExists`** for `my-bucket` — globally-unique name taken. Expected.

For these, the ONLY code work is **graceful handling** (see BUG-05): detect, explain in plain English, mark the run failed, record it, clean up state — never dump a raw stack trace as the only output.

Everything below IS a real defect to fix.

---

## Bugs to fix (priority order)

### BUG-01 — Router classifies read-only questions as DESTRUCTIVE workflows  🔴 CRITICAL (safety)
This is the top priority — it's a safety defect, and it appears to be a **regression** against Phase 2/6, where routing was live-verified (PROGRESS: "How many instances… → query_ec2_instances 0.95").
- Screenshot **20**: "How many s3 buckets are running in aws?" → `Classified → destroy_vpc`.
- Screenshot **19**: "Are any instances up and running in azure or gcp?" → `Classified → destroy_vpc`.
- Contrast screenshot **18**: "How many instances are up and running in aws right now?" → correctly `query_ec2_instances (0.95)`. So the router is **inconsistent**, not uniformly broken.

Required: any read/inventory/status question ("how many…", "are any… running", "list…", "did I create…", "what is the … of…") must route to a **read-only** intent and MUST NEVER map to `destroy_*` or `provision_*`. Destructive intents require an explicit action verb (delete/destroy/remove/tear down). Add a hard guard so a read-classified query can never enter a destructive workflow even if the LLM misfires. Log reason+confidence; on low confidence or ambiguity, ask for clarification. Add regression tests to `test_routing_scenarios.py` using these exact three prompts.

### BUG-02 — GCP workflow receives an AWS machine type  🔴 HIGH
Screenshot **12** log: `machine_type = "ec2-micro"` on `google_compute_instance`. GCP shapes are `e2-micro` / `e2-medium` / `n2-standard-2` (screenshot 12's own prompt lists them). An AWS-style value leaked into the GCP plan — a Phase 3/5 param-mapping defect. Enforce per-cloud param schemas so AWS `instance_type`, Azure `size`, and GCP `machine_type` are validated against that cloud's allowed shapes and can't cross over. Add a test asserting a GCP VM plan never contains `ec2-*`.

### BUG-03 — Streaming crash: TransferEncodingError  🔴 HIGH
Screenshot **15**: `Agent run failed: Response payload is not completed: <TransferEncodingError: 400, 'Not enough data to satisfy transfer length header'>`, answer cut off mid-sentence. This is on the Gemini→backend→SSE path (note PROGRESS already fixed one SSE framing bug — CRLF split — so treat this as a *different*, upstream-completion issue). Ensure chunked-transfer/streaming completes or fails cleanly; on upstream truncation emit a proper `error` event and let the client auto-retry/resume via Last-Event-ID (mechanism already exists per PROGRESS 6.3) instead of a hard crash. Don't leave the timeline half-finished. Add a test simulating a truncated upstream stream.

### BUG-04 — Inconsistent context-graph / inventory recall  🟠 MEDIUM
Recall works in screenshots **5** and **6** (correctly recalls `sai-test — aws ec2 — run 0849a16f`) but fails in **14** and **16**: "Did I create any resources in aws or azure or gcp?" → "I couldn't find a resource matching 'all resources'… I won't guess." The Phase-4 `inventory.resolve()` is literal-matching the phrase "all resources" instead of treating a broad inventory question as "list everything for this session/org." Fix: broad/plural inventory queries aggregate ALL resources across clouds for the session/org and return a list; specific queries still resolve by name/context. Consistent across AWS/Azure/GCP. Add tests to `test_inventory.py` for the broad-list case.

### BUG-05 — Raw provider errors dumped instead of handled gracefully  🟠 MEDIUM (big UX win)
Screenshots **9** (Azure 403), **12** (GCP 403), and the S3 409 all end with a raw stack trace in the Logs tab and a run that just stops. For EVERY apply failure, add a provider-error handler that:
1. Classifies common failures — `AuthorizationFailed`/403 (IAM), `SERVICE_DISABLED` (API off), `BucketAlreadyExists`/409 (name taken), quota, already-exists, invalid region/zone, expired/short-lived creds (your sandbox creds are ~1h per PROGRESS).
2. Posts a short human-readable message in the **conversation**: what failed, likely cause, exact next step ("Enable the Compute Engine API at <url>", "Grant the SP Contributor on the subscription", "Choose a globally-unique bucket name", "Cloud credentials look expired — refresh .env").
3. Marks the Timeline node **failed** (red), writes failure+reason to the **context graph**, resolves the **ServiceNow** record as failed.
4. Ensures no dangling Terraform state / partial resources; report what remains.
Keep the raw logs in the Logs tab (that's good) — just never let the raw trace be the only thing the user sees. Add tests mapping each error signature → the classified, friendly outcome.

### BUG-06 — Timeline placeholder text "Agent Agent / Processed request"  🟡 LOW
Screenshots **15** and **16**: a timeline node renders `Agent Agent` with body "Processed request" — placeholder leaking through instead of the real agent name + step description. Fix the node label/description mapping so it shows e.g. "CloudOps Agent" / a real summary.

### BUG-07 — S3 bucket-name UX  🟡 LOW
Enforce the 3–63 lowercase rule the UI already states (screenshot 4 used `my-bucket`), warn that names are globally unique, catch the 409, and ask for a new name rather than failing raw.

---

## Do NOT treat these as bugs (they are correct behavior — keep them)
- Screenshot **8**: Pydantic rejecting an invalid OS value ("must be ubuntu-22.04 or ubuntu-24.04"). This is correct validation from Phase 3 — do not loosen it.
- The three provider 403/409 failures themselves — the app *should* surface them; only the *handling* (BUG-05) needs work.
- Secret redaction (`private_key_pem = ••••REDACTED••••`, screenshots 1, 17) — working correctly, keep it.

## Also verify still-green (regression guards — see PROGRESS "What is WORKING")
AWS EC2 provision + S3 provision (unique name) plan→approve→apply→verify (screenshots 1–5, 17, 18); per-message artifact-panel binding; live timeline with real per-node timings; day-2 port-add via approval (screenshot 17); recall in the working cases (5, 6). Keep the 210/20/9 test suites green and add the new tests above.

## How to work
- Fix one bug at a time. For each: state the root cause, whether it's a regression vs a gap, the fix, and how to verify it against the specific screenshot/prompt. Then **update `PROGRESS.md`** (new dated section: "Phase 7 — post-Opus-4.8 fix pass", mark each bug fixed with its verification).
- Prefer real, in-environment verification (the stack + live creds) exactly as PROGRESS describes; add automated tests so these can't regress again.
- Don't start the still-pending build items (DevOps agent end-to-end §H, SRE §I, full RAG UI §K, checklist section D/E/F boxes) until BUG-01…07 are fixed and verified. After the fix pass, we'll resume the roadmap.

## Verification checklist (reproduce with these exact prompts)
- "How many s3 buckets are running in aws?" → read-only intent, returns a count; NOT `destroy_vpc` (was #20).
- "Are any instances up and running in azure or gcp?" → read-only inventory; NOT `destroy_vpc` (was #19).
- "Did I create any resources in aws or azure or gcp?" → lists all provisioned resources for the session (was #14, #16).
- "Create a Vm in gcp" → asks GCP machine types; plan uses a valid GCP `machine_type`, never `ec2-*` (was #12).
- Force provider failures (S3 `my-bucket`; GCP with Compute API off; Azure SP without Contributor) → friendly explanation + next step in chat, Timeline node failed, ServiceNow closed failed, no dangling state (was #9, #12).
- Long streaming answer → completes without TransferEncodingError; upstream drop → clean retry/resume (was #15).
- Timeline nodes show real agent names, never "Agent Agent" (was #15, #16).
- Regression: AWS EC2 + S3(unique) still go plan→approve→apply→verify; 210/20/9 suites still green.

Start by reading `PROGRESS.md` and `Screenshots/SCREENSHOT_INDEX.md`, then reply with: (a) confirmation of which screenshot failures are provider-side vs. real code bugs, and (b) your root-cause hypothesis + fix plan for **BUG-01** (the router safety regression) before writing any code.

---

## Note to you (the human)
Three failures need YOUR action outside the code — Fable 5 can only make the app handle them gracefully:
- **GCP:** enable the Compute Engine API on the project (screenshot 12).
- **Azure:** grant the service principal **Contributor** on the subscription (screenshot 9; already noted as a known gap in your PROGRESS Phase 5).
- **S3:** use a globally-unique bucket name (not `my-bucket`).
Also: your sandbox cloud creds are ~1h TTL per PROGRESS — make sure `.env` has fresh AWS/Azure/GCP/Gemini creds before Fable 5 runs live verification, or it'll hit false "failures."
