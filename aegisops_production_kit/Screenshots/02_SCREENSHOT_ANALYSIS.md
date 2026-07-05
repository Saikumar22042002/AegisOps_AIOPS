# 02_SCREENSHOT_ANALYSIS.md — in-depth reading of all screenshots

Captured across manual UI testing. Each entry: what it shows, and whether it's fixed / new / systemic. Reference by number.

## What earlier passes FIXED (regression guards — do NOT break; add tests to lock in)
- **8** — Azure apply 403 → graceful plain-English message ("service principal doesn't have permission… grant Contributor… state check: 1 resource remains…"). Keep.
- **9** — GCP apply SERVICE_DISABLED → graceful message with the activation URL + "retry from a clean plan" + state check. Keep.
- **11** — "How many Vm in aws now?" → real inventory across AWS+GCP (instance ids, regions, timestamps) + Terraform policy panel (IMDSv2/encrypted/private subnet). Keep.
- **12** — destroy→recreate: `3 destroyed` then re-create in logs. Keep.
- **13, 14** — "delete/destroy the s3" → routes to `delete_s3_bucket`. Keep.
- **15** — "destroy the Vm in aws" → `destroy_ec2`; asks for the instance identity before acting; run reaches `completed · 5.1s`. Keep (but see N-08 — destroy misfires in other cases).
- **3, 4, 5** — S3 with a unique name (`sai2792002-bucket`) → plan +4 → approve → apply. Keep.

## NEW / STILL-BROKEN bugs

### N-08 — Action/workflow safety: create and destroy are being SWAPPED  🔴 CRITICAL, DESTRUCTIVE (top priority)
Reported in manual testing (both directions):
- Asking to **create** a new instance **deleted the previous** instance. ← destructive: a benign create tore down real infra the user never named.
- Asking to **destroy** a VM started a **provision** workflow instead.
Note screenshots 12/15 show EC2 destroy working correctly in *some* cases, so this is intermittent / phrasing- or path-specific, not uniform — which is exactly why it needs invariant-level tests, not a one-off fix.
- Most likely root causes: (1) **shared/reused Terraform state** — a second `apply` in the same workspace sees the first resource in state and, since config differs, destroys+recreates it (prime suspect for "create deletes previous"); (2) **router misclassification** flipping create↔destroy.
- Expected: create only creates (own isolated state); destroy only destroys the confirmed target; a hard guard blocks any action-vs-Terraform-operation mismatch; a create plan containing destroy actions halts. Tests cover both directions across every resource type. (Requirements §1.)

### N-03 — Conversational memory/continuity broken  🔴 CRITICAL (product-defining)
- **16** — "What is my previous question?" → `get_conversation_history` → General Agent → "This is the beginning of our conversation, you have not asked a previous question yet."
- **18** — "I've asked several queries… are you losing memory?" → "my context window is currently blank… I cannot recall your previous queries… re-paste or summarize."
- **17** — user's frustration: "there is no continuity."
- Root cause: conversation history isn't being passed into the LLM. Expected: full session transcript threaded into every call, summarized for long threads; references resolved against transcript AND inventory; ChatGPT/Claude-level within-session continuity. (Requirements §2.)

### N-02 — Provisioned instance not reachable (SSH dead)  🔴 HIGH
- **1** — reply: "Instance ready. Connect: `ssh ubuntu@…`; retrieve key via `terraform output -raw private_key_pem`." Logs show `ingress_ports = tolist([])` (SG opens nothing).
- **2** — PowerShell: `ssh ubuntu@ec2-44-200-185-56… port 22: Connection timed out`.
- Expected: usable VM — open 22/3389 to a collected CIDR, deliver the key/password via secure UI reveal, working connect instructions. (Requirements §3.)

### N-01 — "Verification" never completes (hangs)  🔴 HIGH
- **4, 5** (S3) and **19** (GCP) — timeline stalls on **Verification** spinner after everything above is green; run never finalizes. Contrast **15** (plan-only) which reaches `completed`. So verification hangs specifically on **apply** runs.
- Expected: verify → green → finalize; timeout → warned/failed; never infinite spinner. (Requirements §4.)

### N-06 — Provision result not surfaced in chat  🟠 MEDIUM
- **3** — S3 logs show `bucket_name`, `bucket_arn`, verification ok, but the conversation posts no "bucket ready" summary (contrast the EC2 connection summary).
- Expected: every success posts a resource-appropriate card. (Requirements §3.)

### N-05 — Azure module too restrictive vs. platform reality  🟠 MEDIUM
- **6** — app: "os must be ubuntu-22.04 or ubuntu-24.04" (rejects Windows; narrow size set).
- **7** — Azure portal, same sandbox: creating a **Windows** VM, D-series, default resource group — clearly allowed.
- Expected: support Windows + Ubuntu/Debian and B/D/E-series; default RG; align choice lists with reality. (Requirements §5.)

### N-04 — Markdown not rendered  🟠 MEDIUM
- **8, 9, 11, 15** — literal `**bold**`, `###`, backticks shown as raw text.
- Expected: full markdown rendering like ChatGPT/Claude. (Requirements §6.)

### N-07 — Response quality/voice  🟠 MEDIUM
- **15, 16, 18** — the message bubble duplicates the timeline Finalize text verbatim and reads verbosely.
- Expected: concise, structured, consistent voice; no message/timeline duplication. (Requirements §6.)

## Systemic themes (your words: "keeps repeating; looks like a vibe-coding demo; should rival ChatGPT/Claude")
The repetition is the symptom of manual-only testing. The cure is the Phase-A automated suite in `03_TEST_MATRIX.md` — encode the safety invariants (N-08), memory (N-03), verification (N-01), routing, secrets, and rendering so regressions fail a test instead of surfacing weeks later in the UI. You noted testing is partial, so the suite must cover the whole matrix, not just these images.
```
