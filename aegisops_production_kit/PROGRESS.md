# AegisOps — Build Progress

Mirrors `docs/06_FEATURE_CHECKLIST.md`. A box is checked only when backed by **real**
systems (no mock/stub/placeholder). Updated as each milestone lands.

## Milestone status
- [x] **M1 — Foundation & ops** _(live-verified: full stack healthy, realm imported)_
- [x] **M2 — Auth + design system + app shell** _(live-verified: real OIDC + RBAC; UI pixel-checked dark/light/mobile)_
- [x] **M3 — Data layer + integration clients** _(live-verified: migrations, seed, /integrations health, TerraformRunner plan)_
- [x] **M4 — LangGraph core + CloudOps/DevOps/SRE agents** _(graph compiles, Postgres checkpointer, SSE streams end-to-end; multi-cloud templates; full Gemini flow activates with key)_
- [x] **M5 — Workspace wiring + module endpoints** _(live-verified: real chat SSE + two-tab + approval + feedback; modules/admin bound to APIs; integrations grid live health)_
- [x] **M6 — Observability, hardening, tests, docs** _(Langfuse+OTel per-run wired; tests green: pytest 22, vitest 4, playwright smoke; README; no banned tokens)_

---

## Frontend rendering & session fixes (2026-06-30)
Root cause of the "dead UI": the POST-SSE client split frames on `"\n\n"`, but sse-starlette
emits `\r\n\r\n` — so `onEvent` never fired and no chat/artifact/approval state ever updated.
Backend, agents, Terraform runner, approval gate were already correct (verified from raw bytes).
- [x] **SSE render (P1)** — `lib/sse.ts` normalizes CRLF→LF before frame split (+ trailing-frame
      flush). step/token/analysis/confidentiality/console/interrupt/done now drive the UI live.
- [x] **Auth (P2)** — verified live: `/auth/login`→cookie, `/auth/me`→200 real user, CORS+creds OK.
      Earlier `ERR_EMPTY_RESPONSE` was a transient container recreate, not a code bug. Approve UI
      RBAC-gated on `can_approve`.
- [x] **Sessions (P3)** — frontend creates a session on first message and **reuses** its id for the
      whole thread (was sending `sessionId:null` → a new session per message). History load, rename
      (`PATCH /sessions/{id}`), delete (`DELETE /sessions/{id}`), reload persistence (localStorage +
      `restoreLast`).
- [x] **Sidebar (P4)** — real `GET /sessions` grouped Today/Yesterday/Earlier; `GET /overview`
      drives Projects + Incidents badges and org name; real empty state. No hardcoded rows.
- [x] **Artifact panel (P5)** — already wired to `/runs/{id}/{tab}`; now refetches on an
      `artifactNonce` bump (run start/interrupt/done/approval) and overlays live console lines in Logs.
- [x] **Approval gate (P6)** — inline Approve/Reject (RBAC-gated) → `POST /approvals/{runId}` resume
      stream; Timeline/Logs/Approvals tabs refresh on resolution.

---

## Per-message run integrity & history (Phase 1 — 2026-07-03)
Root cause of the mismatched artifact panel: the panel was bound to a single global `activeRunId`
that only advanced on `interrupt`/`done` and was set to the *latest* run on session open — so it
showed a stale/latest-only run, never the run of the message being viewed, and never updated live
during a new query. Backend already persisted `message.run_id` + reconstructs `/runs/{id}/*` from
the DB; the defect was purely in binding. Fix:
- [x] **Run identity emitted first (backend)** — `POST /chat` and `POST /approvals/{id}` now lead the
      SSE stream with a `run` event `{runId, sessionId}` (before any step/token). `_sse` de-dupes by
      event id so the early emit is delivered exactly once. Each assistant message links to its run
      from the first event. _(live-verified: `run` is always the first event, exactly once.)_
- [x] **Per-message selection (frontend)** — store gains `selectedMessageId` + `selectMessage()`. The
      artifact panel binds to the **selected/active message's** run (fetches `/runs/{runId}/{tab}`),
      never a global latest. Clicking any past message pins the panel to THAT message's run; a new
      query auto-follows live. Each restored message keeps its own `run_id`.
- [x] **Live timeline** — while a run streams, the Timeline renders from the graph's `step` events in
      real time; on `done` it hands off to the persisted `/runs/{id}/timeline` (identical node visual).
      Logs overlay the live console on the persisted snapshot.
- [x] **History + persistence** — `openSession` restores every message with its own run and defaults
      the panel to the newest run; clicking earlier messages loads their distinct runs. Runs persist
      and remain retrievable after reload (survives restart).
- [x] **Verified end-to-end (real UI, Playwright, 9/9):** in a real 11-message conversation, clicking
      greeting/EC2/RDS/capabilities each loaded its **own** distinct run (`0193dbe3`/`93e330e7`/
      `9ba71385`/`2a7091b0`) — the capabilities message now shows `query_platform_capabilities`
      (Knowledge Agent), **not** the RDS run (the reported bug). Full reload persisted the session +
      per-message runs; re-selection still loaded the correct run. Backend contract check: 3 queries in
      one session → 3 distinct run ids, each persisted on its message, each retrievable via `/runs/*`.
      _(Note: sandbox Gemini/AWS creds were expired during this run, so new live answers/plans error;
      binding/persistence are creds-independent and were verified against real runs persisted earlier.)_
- [x] **Re-verified LIVE (2026-07-03, creds rotated, Playwright 12/12):** 3 distinct **live** runs in one
      session — a knowledge answer, a real **S3 `terraform plan` → approval gate**, and a second knowledge
      answer. Confirmed: each message fetched its **own** run from `/runs/{id}` on click; the 3 runs are
      distinct (panel is not latest-only); re-selecting message #0 rebinds the panel to run #0 (not the
      latest); the live panel auto-followed the new run and streamed steps in real time; and a full page
      reload persisted the session + every per-message run binding. 12/12 checks passed.

---

## Timeline timings (Task B) + latency audit (Task C) — 2026-07-03
- [x] **Real per-node timings (B)** — new `agents/timing.py` records each graph node's real
      `started_at`/`ended_at` into the `run_steps` table (previously unused). `router`, `execute`,
      `verify`, `finalize`, `servicenow_update`, `notify` timed via a `_timed` wrapper in `graph.py`;
      `cloudops_plan` records finer sub-steps (`cloudops_agent`, `policy_evaluation`, `planner`);
      the `approval` node self-times **across the human-in-the-loop interrupt** (upsert preserves the
      first start), so "Human Approval" shows the true wall-clock wait. `/runs/{id}/timeline` now
      sources durations + a `total` from `run_steps` (hardcoded "0.3s"/"0.4s" removed; legacy runs
      without timings show "—", never a fake value). A failed apply now reads **failed** (was "queued").
      Frontend timeline unchanged in layout (matches source HTML) — the existing right-aligned mono
      time slot now shows real durations; header shows `elapsed · total · mode`.
      _(Live-verified: applied-S3 run showed Router 2.9s · Cloudops Agent 3.3s · Policy 4ms · Planner
      40.2s · Human Approval 5.2s · Execute 15.3s · Finalize 14ms · total 1m07s. Completed knowledge
      run rendered in the UI: Router 3.7s · Knowledge Agent 3.9s · Finalize 17ms · total 7.6s.)_
- [x] **Provisioning latency audit (C, report-only)** — measured warm terraform ops (providers
      already cached, `.terraform` persisted): S3 `init` ~13–16s, S3 `plan` (no data sources) ~14s,
      EC2 `init` ~19s, EC2 `plan` (AMI/VPC/subnet reads + refresh) ~21s. Findings: (1) `cloudops.py`
      runs `terraform init` on **every** request unconditionally — safe to skip when the workspace is
      already initialized (`.terraform/` + lockfile present), saving ~13–19s/request. (2) The TF
      workspace/state lives on a **OneDrive bind-mount**, which massively amplifies terraform's
      many-small-file I/O; moving state + a `TF_PLUGIN_CACHE_DIR` to a Docker named volume would cut
      init/plan time. (3) State refresh on plan, AWS data-source reads, and actual EC2 boot are
      **inherent** and must not be disabled (correctness). No code changed — approval gate untouched.

---

## Phase 2 — Multi-cloud routing + Terraform integrity — 2026-07-03
Problem: CloudOps treated nearly every request as AWS — `cloudops.py` did `cloud = state.get("cloud") or "aws"`
and `templates.select()` had a **cross-cloud fallback** (a resource-only match), so "create a VM in Azure"
(intent `create_azure_vm`) selected `aws.ec2`. Fixes:
- [x] **2.1 Cloud resolution** — new `resolve_cloud(state)` with explicit priority: (a) cloud named in the
      request (router-extracted), (b) UI cloud selector, (c) resource-vocab hint (only cloud-branded
      services: s3/rds/vpc/eks→aws, storage/resource_group→azure, gcs→gcp — **compute/"VM" is NOT hinted**).
      Ambiguous → **asks which cloud**, never silently defaults to AWS. Resolved cloud + reason are logged
      and emitted to the timeline ("Target cloud · AZURE (named in request)").
- [x] **2.2 Cloud-safe workflow/module selection** — `templates.select()` now returns the **exact**
      `cloud.resource` template or `None`; the cross-cloud fallback is removed, so wrong-cloud execution is
      structurally impossible. A cloud with no approved module → honest clarification (never a wrong-cloud plan).
- [x] **2.3 Terraform integrity audit + refactor** — Audited: the 8 curated modules are pre-written,
      version-pinned, secure-by-default HCL under `infra/terraform-workspaces/`; user inputs reach Terraform
      strictly as `-var` (never written into module files); the LLM only classifies + extracts values. **One
      unsafe path found & removed:** `generic.module` / `_generate_generic_workspace()` string-formatted a
      `main.tf` at runtime with an LLM-extracted module `source`. Deleted the codegen + the `generic.module`
      template. All provisioning now runs through fixed, reviewed modules — the LLM never authors/templates HCL.
- [x] **2.4 Module coverage inventory (honest):** AWS — EC2, S3, RDS, VPC, EKS (all real). Azure — Storage
      Account, Resource Group (real); **VM / DB / AKS missing**. GCP — GCS (real); **VM / Cloud SQL / GKE
      missing**. `demo-null` real (local test only). Missing combos are surfaced as clarifications, not faked.
- [x] **2.5 Verified (live, self-run):** "VM in Azure" (UI=AWS) → resolves **Azure**, clarifies "no azure
      module" (NOT aws.ec2); "EC2 in AWS" → plans `aws.ec2`; "VM in GCP" (UI=AWS) → resolves **GCP**, clarifies;
      ambiguous "provision a virtual machine" (no selector) → **asks which cloud**; "S3 bucket" → plans `aws.s3`.
      UI label + generated plan both match the resolved cloud. Approval gate unchanged. Tests: `test_templates.py`
      updated to assert cloud-safety + no generic fallback (verified against live `select()`).

---

## Phase 3 — Cloud-agnostic parameter collection + usable credentials — 2026-07-03
Problem: provisioning used hardcoded defaults (t3.micro / aegisops-vm / fixed AMI) and `key_name =
(known after apply)` → instances had no usable SSH key; the agent never asked for required inputs.
- [x] **3.1 Reusable param framework** — `agents/params.py`: a cloud-agnostic `ParamSpec` (name→TF var,
      required vs defaulted, type, choices, default, help) with per-module declarations for every REAL
      module (AWS EC2/S3/RDS/VPC/EKS, Azure Resource Group, GCP GCS). Adding a module = add its ParamSpec
      list (+ schema + workspace). No schemas for not-yet-built modules (Azure VM/DB/AKS, GCP VM/SQL/GKE).
- [x] **3.2 Ask only what needs a decision** — `missing_required()` returns only decision-critical params
      with no safe default. EC2 asks **name, instance_type, OS, key pair**; region/VPC/subnet/root-volume
      default silently and are overridable (VPC/subnet → account default unless named). _(verified: EC2
      asks exactly [name, instance_type, os, key_pair], never vpc_id/subnet_id.)_
- [x] **3.3 Interactive collection before planning** — CloudOps detects missing required params, asks for
      exactly those (structured `params` SSE event → styled "Required to proceed" card, matching the source
      HTML card idiom), parses free-form/NL + Gemini extraction (OS synonyms, "create" key), and validates
      against the Pydantic schema. Invalid → **specific per-field clarification, no plan**. Multi-turn:
      collection spans chat turns via a Redis pending record keyed by session; the router detects it and
      continues the same request (reuses the ticket, no re-classification). Collected params persist on the
      run (`input_json`) + the request persists on the message (`analysis.param_request`) for later viewing.
- [x] **3.4 Usable credentials / key pair** — `aws-ec2` module gains `key_name` + `create_key_pair`
      (tls_private_key + aws_key_pair); `key_name` is always a real value (existing name, or a generated
      `<name>-key`) — never "known after apply". After apply, `verify` surfaces connection details
      (public DNS/IP, login user, key name) via a masked message; the private key is a **sensitive** TF
      output excluded from logs/DB/chat (retrieved out-of-band via `terraform output -raw`). RDS master
      password stays AWS-managed (Secrets Manager); endpoint surfaced.
- [x] **3.5 No hardcoded defaults on the provisioning path** — name/type/OS/key come from the user (asked);
      the plan/panel/resource reflect the user's chosen values. Region/VPC/subnet/volume are real documented
      defaults, overridable — not literals that ignore input. OS selection is real (AL2023, Ubuntu 22.04/
      24.04, Windows 2022 → per-OS AMI data sources + login user).
- [x] **3.6 Verified — framework (deterministic, real code):** all logic checks pass (asks-only-decision-
      critical, validation, subnet default+override, bogus-type per-field error, key-pair→real key_name,
      all 7 modules wired, Redis multi-turn). `aws-ec2` HCL `terraform validate` clean.
- [x] **3.6 Verified LIVE (2026-07-04, real Gemini + AWS acct 939338074907):**
      (1) "create an EC2 instance" → asks only [name, instance_type, os, key_pair], not VPC/subnet, no plan;
      (2) "name web-01, t3.small, ubuntu, key my-key" → router resumes the pending request, validates, and
      plans `aws.ec2` with exactly those values (subnet defaulted); (3) "…in subnet subnet-009a…" → override
      captured in the plan; (4) "type banana" → specific "**Instance type** — 'banana' is not a valid…",
      no plan; (5) **apply** (create key pair) → instance `i-0b3f6a37…` with real key `p3-demo-key`,
      connection surfaced (`ssh ec2-user@ec2-3-230-172-19…`), **private key NOT leaked** (only the sensitive
      output name is referenced; PEM material absent from outcome/logs/DB), then destroyed clean; (6) all 7
      modules ask only their decision-critical params. Styled "Required to proceed" card confirmed in the UI.
      Live-test bug fixed: default root volume was 8GB < the AMI's 30GB snapshot → now defaults to the image's
      own size (`null`), overridable.
- [x] **Re-confirmed pre-Phase-4 (2026-07-04):** (a) "create an EC2" asks only [name, instance_type, os,
      key_pair], not VPC/subnet; (b) specifying a subnet overrides the default (captured in the plan);
      (c) after apply a real usable key pair + connection details (public IP/DNS, login user, key ref) are
      surfaced (verified via the live apply of `i-0b3f6a37…`). Behaviors hold.

---

## Phase 4 — Resource state & memory (day-2 operations) — 2026-07-04
Problem: the agent had no memory of what it provisioned — "what's the VPC id of the instance I created"
returned a generic account discovery, and "add ports to test-vm" had nothing to act on.
- [x] **4.1 Persist every provisioned resource** — new `resources` table (migration `0002_resources`) +
      `agents/inventory.py`. On a successful apply, `cloudops_execute` records the resource: stable name,
      cloud, region, resource type, provider id (instance/VPC id), key attributes (IPs, VPC, subnet, SG,
      key pair, tags — from real TF outputs), the validated inputs (to rebuild a modify plan), the TF
      workspace, owning run+session, status, timestamps. Org-scoped.
- [x] **4.2 Resolve references to prior resources** — `inventory.resolve()` matches by exact/substring
      name ("test-vm") or by context ("the instance I created" → most-recent active, type-filtered).
      Not found or ambiguous → the agent asks instead of guessing. The router now extracts a `target`
      reference and an action (create|read|modify|destroy); read/modify with a target hit the inventory,
      returning that resource's **real recorded values**, not a generic discovery.
- [x] **4.3 Day-2 operations** — `aws-ec2` module gained a managed security group with a day-2-modifiable
      `ingress_ports` variable + `vpc_id`/`subnet_id`/`security_group_id` outputs. Read-back queries (VPC/
      subnet/IP/status) run with no approval; "add inbound ports 8501,8502 to test-vm" resolves the real
      resource, rebuilds its module inputs + merged ports, plans the SG change, and goes through the
      **human-approval gate** → apply → updates TF state + inventory. Never LLM-authored HCL.
- [x] **4.4 Consistency** — reads reconcile live via read-only SDK calls (EC2 describe → refresh IPs/state);
      terminated/destroyed resources are marked and no longer resolved/offered for day-2 actions.
- [x] **4.5 Verified — inventory core (deterministic, real code):** record→persist (provider id +
      attributes); resolve by name + by context ("the instance I just created" → real recorded VPC);
      non-existent → none (agent asks); modify-merge → `ingress_ports=[8501,8502]`; destroyed → no longer
      resolvable. `aws-ec2` SG/outputs `terraform validate` clean.
- [x] **4.5 Verified LIVE (2026-07-04, real Gemini + AWS acct 147935447701):** in one session —
      (1) created **test-vm** → written to **both stores**: DB inventory (`i-0e548cbe155c1b2e1`,
      `vpc-0d22ef2487a3ae2d6`, `subnet-036ab875202083527`, run+session linked) AND the Neo4j context graph
      with `resource↔run↔session` relationships; (2) "VPC id of test-vm" → returned its **real recorded
      values** (vpc/subnet/private+public IP/DNS), not a generic discovery; (3) "subnet of the instance I
      just created" → context-match to the same real resource; (4) "add inbound ports 8501,8502 to test-vm"
      → resolved it, planned the SG change, **approval gate** → applied → inventory `ingress_ports=[8501,8502]`
      updated; (5) non-existent "ghost-server-99" → "I couldn't find… I won't guess"; then destroyed clean.
      Resource facts are deterministic (from the stores, reconciled via SDK); the LLM only maps phrasing → a
      lookup key and never invents an id/VPC/subnet.

---

## Phase 5 — Multi-cloud service catalog expansion — 2026-07-04
Built 6 new real, version-pinned Terraform modules + Pydantic schemas + param specs + registry entries,
all wired into the existing frameworks (Phase 2 routing, Phase 3 collection/credentials, Phase 4
inventory/context-graph/day-2/approval). No LLM-authored HCL.
- [x] **5.1 New modules** — Azure: `azure-vm` (Linux VM, generated SSH key, day-2 NSG ports), `azure-postgres`
      (PostgreSQL Flexible Server, generated admin password), `azure-aks` (managed cluster, sensitive
      kubeconfig). GCP: `gcp-gce` (Compute VM, generated SSH key, day-2 firewall ports), `gcp-gke`
      (cluster + node pool), `gcp-cloudsql` (Postgres, generated root password). All secrets are sensitive
      TF outputs, excluded from logs/DB/chat. `terraform validate` clean on all 6.
- [x] **5.2 Consistency** — cloud-aware synonyms in `templates.select` route "VM"/"database"/"k8s" to the
      right per-cloud module (aws.ec2/azure.vm/gcp.vm; aws.rds/azure.postgres/gcp.cloudsql; aws.eks/azure.aks/
      gcp.gke). Each collects that module's real params; VMs generate SSH keys; GCP project auto-fills.
      TerraformRunner now passes Azure (`ARM_*`) + GCP (`GOOGLE_*` + mounted SA key) creds. Day-2 read works
      cross-cloud; day-2 modify extended to Azure/GCP VMs.
- [x] **5.4 Verified live** — GCP VM **full lifecycle** (create→apply `104.198.229.218`→read→add-port-8080→
      destroy), recorded in **both stores** (DB + graph resource↔run↔session). All 6 modules produce **real
      cloud-specific plans** with valid creds (Azure RPs registered; GCP APIs enabled). Apply-deferred where
      the sandbox limits: Azure apply blocked by the SP lacking Contributor (can't create resource groups);
      Azure/GCP managed-DB + k8s applies are slow/costly — plans proven, applies deferred (not faked).

### Supported matrix (cloud × service × status) — honest, current
| Cloud | Compute / VM | Object storage | Managed DB | Kubernetes | Network / other |
|-------|--------------|----------------|------------|------------|-----------------|
| AWS   | EC2 ✅ apply | S3 ✅ apply     | RDS ✅ plan | EKS ✅ plan | VPC ✅ plan     |
| Azure | VM 🅿️ plan   | Storage ✅ plan | PostgreSQL 🅿️ plan | AKS 🅿️ plan | Resource Group 🅿️ plan |
| GCP   | Compute ✅ apply | GCS ✅ plan  | Cloud SQL 🅿️ plan | GKE 🅿️ plan | — |

Legend: **✅ apply** = full create→apply(→destroy) verified live; **✅ plan / 🅿️ plan** = real cloud-specific
plan verified live, apply-deferred. AWS EC2 + GCP Compute are fully apply-verified (Phase 3/4/5). Azure
applies are **apply-deferred pending an SP with Contributor** (current sandbox SP is read/plan-only). Managed-DB
and k8s applies are deferred for cost/time; their plans are real. `azure-storage`/`azure-rg`/`aws-s3` etc.
were apply-verified in earlier phases where creds allowed.

### Add-a-new-service pattern (documented in docs/08_SERVICE_CATALOG.md)
1. `infra/terraform-workspaces/<cloud>-<svc>/main.tf` — real, version-pinned HCL (secrets as sensitive outputs).
2. `schemas/workflows.py` — a `WorkflowInputs` subclass (types, validators, safe defaults).
3. `agents/templates.py` — a `WorkflowTemplate(...)` entry + policy_fn (+ synonyms if needed).
4. `agents/params.py` — a `ParamSpec` list marking only decision-critical params required.
   Inventory + context-graph recording, day-2 read, and the approval gate are inherited automatically.

---

## Phase 6 — Comprehensive test suite + platform hardening — 2026-07-05
Built a real, runnable, wide-coverage test suite across the whole platform and fixed every defect it
exposed. All suites run against the REAL environment (containerized PG/Redis/Neo4j + live cloud creds),
no mocks of the systems under test.

### 6.1 — Test infrastructure (make test genuinely executes)
- [x] **Root cause fixed:** the slim prod image installs `pip install .` (no dev extras) so it had **no
      pytest** — `make test` could not run in the environment. Added an `api-test` compose service
      (profile `test`, `docker-compose.override.yml`) that reuses the built `aegisops-api:local` image,
      adds only the test tools at runtime, mounts host source (`PYTHONPATH=/app` so tests run current
      code), runs as root (pip can install), and points integration tests at the live compose datastores
      by service name. `make test` / `./make.ps1 test` now run backend pytest **in-container** + frontend
      vitest; added `test-backend` / `test-frontend` targets. Prod image stays slim by design.
- [x] **Backend pytest — 210 passing** (was 22), in-container against real PG/Redis/Neo4j + live creds:
      routing/cloud-resolution, per-module param collection, schema validation, the SSE event contract +
      reconnect/exactly-once, the approval/route decision edges, RBAC at side-effecting endpoints, secret
      redaction, idempotency (real Redis), multi-turn pending collection (real Redis), inventory persistence
      + reference resolution (real Postgres), cloud-SDK import guards. Integration fixtures skip cleanly
      when a datastore/flag is absent, so the unit tier stays green on a bare checkout (testcontainers
      remains available for host runs via `AEGISOPS_TEST_USE_TESTCONTAINERS`).
- [x] **Frontend Vitest + RTL — 20 passing:** store message↔run binding, live streaming render,
      artifact-panel-per-message selection, history restore (each message keeps its own run), feedback
      toggle, confidentiality badge + param card (RTL), and the SSE client's CRLF frame parsing (the
      historical "dead UI" regression guard).
- [x] **E2E Playwright — 9 passing (chromium + Pixel 7 mobile):** real Keycloak password-grant login
      (session reused via `storageState` so tests don't stampede the login rate limiter), workspace load,
      a **real streamed run** driven end-to-end (login → session → POST /chat SSE → live per-message
      timeline), theme cycle, and responsive mobile login+workspace.

### 6.2 — Scenario coverage (the "hundreds of cases" concern)
- [x] **Parametrized routing over every category × cloud** (`test_routing_scenarios.py`, ~55 cases):
      create compute/object-storage/managed-DB/k8s/network on AWS/Azure/GCP in canonical + synonym
      phrasings all resolve to the correct module; resource-brand hints; ambiguous-cloud → **ask** (never
      default to AWS); unsupported combos → None (honest clarification, never cross-cloud).
- [x] **Per-module param + schema coverage** for all 14 real templates: each asks exactly its
      decision-critical params, defaults the rest, validates/rejects bad values per-field, and the EC2
      key-pair transform yields a real `key_name`.
- [x] **Day-2 categories** (`test_inventory.py`, real PG): record→resolve by exact name / context
      ("the instance I created") / ambiguous (→ ask) / not-found (→ ask, never guess) / destroyed
      (no longer resolvable) / upsert.
- [x] **Live routing re-verified with real Gemini (2026-07-05):** 10 representative requests classified
      + routed correctly (EC2→aws.ec2, "VM in azure"→azure.vm, "GCE in gcp"→gcp.vm, s3→aws.s3, "storage
      account in azure"→azure.storage, "database in gcp"→gcp.cloudsql, EKS→aws.eks, "add inbound ports to
      test-vm"→modify, "VPC id of the instance I created"→read, "provision a virtual machine"→ASK-CLOUD).
      Real `terraform plan` for aws.s3 with fresh creds → +4 (provisioning path live, not faked).

### Bugs found by the suite + fixed
- [x] **Secret redaction leak (security).** `redact()`'s generic key=value pattern had 3 capture groups,
      so it hit the keep-first-and-last branch and **re-appended the secret value after the mask**
      (`password=hunter2` → `password••••REDACTED••••hunter2`); `AWS_SESSION_TOKEN=…` / `"SessionToken":…`
      / `AccessKeyId` were not matched at all (compound/underscored/quoted names + STS `ASIA…` ids). Fixed:
      2-group value-masking pattern covering session-token/access-key/private-key, quoted-JSON values, and
      `ASIA` ids; `redact_dict` sensitive-key set widened. Covered by `test_redaction.py`.
- [x] **`azure.storage` had no ParamSpec.** It was registered + routable ("create a storage account") but
      collected nothing, so users hit a cryptic Pydantic error instead of the "Required to proceed" card.
      Added its param spec (account_name + resource_group required).
- [x] **Azure discovery SDK import broken.** `azure-mgmt-resource` 26.x relocated
      `ResourceManagementClient` to `azure.mgmt.resource.resources`; the old import failed so `_HAVE_AZURE`
      was False and the availability precheck **mislabeled Azure as "not configured" even with valid creds**.
      Fixed the import; `azure.enabled`/`ping` now True. Regression-guarded by `test_cloud_tools.py`.
- [x] **Dead code removed:** the `generic.module` runtime-HCL escape hatch (deleted in 2.3) left
      `GenericModuleInputs` + a stale `SCHEMAS` registry in `schemas/workflows.py` and stale docstrings in
      `templates.py`. Removed; the single source of key→schema is `templates._BY_KEY[key].schema`.

### 6.3 — Hardening (verified + tested)
- [x] **SSE reconnect (Last-Event-ID) + exactly-once delivery** — `_sse` de-dupes an event present in both
      the replay buffer and the live queue; the leading `run` event is delivered once and first. Tested.
- [x] **Idempotency** — `claim` blocks a duplicate apply; `store_result`/`get_result`/`release` lifecycle
      verified against real Redis; keys stable + distinct per (run, mode).
- [x] **RBAC at every side-effecting endpoint** — `/chat`, `/approvals/{id}`, `/runs/{id}/input` reject
      unauthenticated (401); non-approvers cannot resolve approvals (403); bad decisions 400 before DB.
- [x] **Secret redaction across logs/streams/DB/context-graph** — free-text `redact()` + structured
      `redact_dict()` both tested; leak bug above fixed.
- [x] **Graceful missing/expired creds** — a cloud with no/broken creds returns a clear "not configured"
      availability check (no crash, no fake); accurate now that the Azure SDK import is fixed.
- [~] **Graph checkpoint/resume after restart** — durable Postgres checkpointer + interrupt/resume is real
      and was live-verified in M4/Phase-1 ("survives restart"); the idempotency + SSE-replay resume-safety
      mechanisms are now unit-tested, but an automated kill-mid-interrupt restart test is **not** added.
- [~] **Rate limiting** — SlowAPIMiddleware is configured (`RATE_LIMIT_PER_MINUTE`); no automated
      load test added (the E2E login-stampede that first exposed it is now avoided via storageData reuse).

### Known gaps (honest)
- Azure / managed-DB / k8s **applies remain apply-deferred** (cost/time); their real cloud-specific plans
  are verified. The Azure SP now authenticates (discovery `ping` OK); a full Azure apply was not run here.
- Full **apply→day-2 lifecycle in the browser** is not automated in Playwright (heavy/creds/cost); it was
  live-verified via the real stack in Phases 3/4/5. E2E covers login→stream→approval-UI→theme/mobile.
- Sandbox cloud creds are **short-lived (≈1h)**; live cloud tests depend on current creds in `.env`
  (AWS `ASIA…` STS token, Azure SP, GCP SA at `infra/secrets/gcp-sa.json`, Gemini key).

---

## Phase 7 — Post-Opus-4.8 fix pass (manual-UI-testing bugs) — 2026-07-05
Evidence: `Screenshots/1.png…20.png` + `Screenshots/SCREENSHOT_INDEX.md`. Every fix verified by new
automated tests AND live against the running stack (real Gemini + AWS/Azure/GCP creds). Suites after
this pass: **backend pytest 284** (was 210), **vitest 20**, **Playwright 9** — all green.

**Provider-side failures (correct rejections, NOT code bugs — only handling was improved):** Azure
`403 AuthorizationFailed` (#9, SP lacks Contributor — infra), GCP `SERVICE_DISABLED` (#12, Compute
Engine API off on the sandbox project — infra), S3 `409 BucketAlreadyExists` for `my-bucket` (#4,
globally-unique namespace). Kept-correct behavior: Pydantic OS rejection (#8), secret redaction
(#1/#17), raw logs staying in the Logs tab, day-2 modify + recall (#5/#6/#17).

- [x] **BUG-01 (CRITICAL, safety) — read questions entered a destroy workflow (#19/#20).**
      **Root cause (from evidence, not the assumed LLM flakiness):** the Router node ran in **6–7ms**
      in both captures — the LLM never executed. A **stale pending parameter-collection record in
      Redis** (armed earlier by a `destroy_vpc` classification, 30-min TTL) unconditionally swallowed
      EVERY later message in the session via the router's continuation short-circuit, re-asking for a
      VPC name (#18 worked because no record was armed). Fixes (new `agents/intent_guard.py`,
      deterministic regex-only — no LLM in the safety loop):
      (1) **Hijack killed** — mid-collection, a question/new-request re-classifies (pending record
      abandoned + logged + surfaced in the timeline); bare param answers ("ok, ubuntu", "name web-01,
      t3.small, key my-key") still continue collection.
      (2) **Hard destructive guard** — post-classification: a question-shaped message can never carry a
      side-effecting action (forced `read` + `query_*` intent); `action=destroy` requires the user's own
      explicit destructive verb, else clarification (action forced read).
      (3) **Belt-and-suspenders in CloudOps** — a destroy plan additionally requires an explicitly
      destructive current message or a pending record stamped `destructive_ok` (only ever set by such a
      message) — structurally invalidating any stale legacy record. Router prompt hardened (questions ⇒
      read; broad questions ⇒ target "all").
      _(Verified live: the exact #19/#20 prompts now classify `read`/`query_*` with real Gemini;
      with the exact stale `destroy_vpc` record seeded in Redis, #19's prompt produced "Set aside the
      pending destroy_vpc", fresh classification, read-only Azure+GCP discovery, record cleared;
      "destroy the vpc named prod-network" still classifies destroy. ~30 new tests incl. the three
      exact screenshot prompts + working-flow answers (#1/#4/#8/#9) that must keep continuing.)_
- [x] **BUG-02 — AWS machine type reached a GCP plan (#12/#13, `machine_type="ec2-micro"`).**
      User typed the AWS-style value; `GCPComputeInputs.machine_type` was an unvalidated `str`. Added
      per-cloud shape validators (schemas/workflows.py): GCP machine types (family allowlist +
      "looks like an AWS instance type" special-case), Azure `size`/`node_size` (`Standard_*`/`Basic_*`),
      RDS `instance_class` (`db.*.*`), Cloud SQL `tier` (`db-*`). Invalid ⇒ the existing per-field
      re-ask — a foreign shape can never reach `terraform plan`. _(Tested incl. the exact #13 answer
      "test-v1, ec2-micro, ubuntu" failing validation through the same call cloudops makes pre-plan.)_
- [x] **BUG-03 — streaming crash `TransferEncodingError` (#15).** The aiohttp truncation escaped
      `general()` (which only caught GeminiError), crashed the graph, and the run was persisted as an
      EMPTY **"completed"** state. `llm.stream_answer` is now truncation-resilient: nothing emitted ⇒
      transparent retry (fresh stream); tokens already shown ⇒ finish cleanly with the partial text +
      visible truncation note + retriable `stream_truncated` error event (client can resume via
      Last-Event-ID); exhausted ⇒ `GeminiError`, which all streaming agents already handle with an
      honest message. `chat._drive` (both drivers) now persists graph failures as **status="failed"**
      with a real message. _(3 new tests simulate the exact live truncation text.)_
- [x] **BUG-04 — broad/typed inventory recall (#14/#16 + #5/#6).** `inventory.resolve()` literal-matched
      "all resources" ⇒ refusal. Now: broad refs ⇒ kind="all" listing (grouped by cloud, honest empty
      state); typed descriptive refs ("the s3 bucket I created") match ONLY that type — never the
      most-recent resource of a different type (#5 recalled the EC2 for an S3 question); recall answers
      now surface size/type + region (#6 asked for instance size and got provenance only). Read path
      rebuilt: resource-aware live counts (EC2 instances/S3 buckets/RDS/EKS/VPCs; Azure VMs/VNets; GCP
      instances via new AggregatedList) across every cloud named in the question + the inventory
      listing; one cloud failing never sinks the others. **Latent defect caught by the new tests
      against real data:** descriptive matching used substrings ("it" in "itest…", "server" in
      "ghost-server-99" triggered fuzzy recall) — now whole-word, whitespace-token based.
      _(Live: #14's exact prompt returns AWS/Azure counts + GCP failure classified + the sai-test
      listing.)_
- [x] **BUG-05 — raw provider stack traces were the only user-visible outcome (#9/#12/S3-409).** New
      `agents/provider_errors.py` classifies failure signatures — Azure `AuthorizationFailed` (⇒ grant
      the SP **Contributor**), GCP `SERVICE_DISABLED` (⇒ exact enable-URL extracted from the error),
      name-taken 409s (⇒ pick a globally-unique name), AWS IAM denials, expired sandbox creds (⇒ refresh
      `.env`), quota, bad region/zone — and every plan/apply/destroy failure now posts a plain-English
      conversation message (what/why/next step), keeps the raw trace in Logs, marks the run failed,
      closes the context graph (failure is terminal), resolves the ServiceNow record as failed, and
      reports leftover Terraform state (`state list`) so partial resources are never silent.
      _(7 signature tests use the exact #9/#12 error texts; live: the real GCP SERVICE_DISABLED came
      back classified with its activation URL.)_
- [x] **BUG-06 — "Agent Agent / Processed request" placeholder (#15/#16).** Timeline builder rendered
      `f"{(run.domain or 'agent').title()} Agent"` + `routing_reason or "Processed request"` — for the
      empty "completed" runs BUG-03 used to persist. Now: real agent names; a domain-less run renders
      "Agent" with the actual failure reason; router/finalize nodes show failed states; `elapsed` reads
      "failed"; frontend Timeline renders the new `failed` node status (red ✕, same idiom as rejected).
- [x] **BUG-07 — S3 name UX (#4).** Read-only `HeadBucket` precheck at plan time: a taken name (403/200
      ⇒ exists) re-asks specifically ("globally unique — prefix with your org/project") before any
      plan; an apply-time 409 falls back to the BUG-05 classifier. _(Live: `bucket_taken("my-bucket") =
      True`, random name = False.)_
- [x] **BONUS (found via the screenshots, governance-grade) — plan chips showed "+0 ~0 -0" for real
      plans (#2/#9/#13/#17 vs logs "Plan: 8 to add").** `show_plan` parsed `terraform show -json`
      through the redacting line-pump; key material in VM plans tripped the maskers, the corrupted JSON
      failed to parse, and `or {}` silently reported zeros — approvers were approving "0 resources" for
      plans that added 8. Now: raw (unredacted) capture for JSON commands — safe because the parsed
      plan is reduced to addresses/actions and `output()` strips sensitive values — and a parse failure
      **raises** instead of reporting a false zero summary. _(Live: a real aws-ec2 plan (key material
      included) parses to +3 with 4 diff entries; unit tests pin the raise-on-unparseable behavior.)_

**Still requiring operator action (outside code, handled gracefully in-product):** grant the Azure SP
Contributor (#9), enable the Compute Engine API on the GCP project (#12) — the exact enable-URL is now
shown in-chat — and keep `.env` creds fresh (≈1h sandbox TTL; expired creds are now classified and
explained instead of crashing).

---

## Phase 8 — Test-first stabilization — 2026-07-05
Method change: the Phase-A matrix suite (`Screenshots/03_TEST_MATRIX.md`) was built BEFORE fixing
anything, run red (37 reds mapping 1:1 to N-01…N-08), then driven green — so every fixed class is
now guarded by a test instead of waiting to resurface in manual UI testing. Suites after this pass:
**backend pytest 377** (+93 over Phase 7; 1 skip = live-cloud flag), **vitest 25**, **Playwright 11**
(+2 mobile-skips by design) — all green against the real stack.

### N-08 — create/destroy swap (CRITICAL, destructive) — root-caused BOTH ways and killed as a class
- **Direction 1 ("create deleted my previous instance") = Terraform state sharing, architecturally
  certain:** every module had ONE local state, so a second create reconciled the same resource
  addresses and destroyed/replaced the first. **Fix: per-resource state isolation** — every create
  plans/applies in its own Terraform workspace (`TF_WORKSPACE=res-<name>`, race-free env-var
  selection; `workspace new` ensured post-init; migration `0003_state_workspace` records the slug on
  the inventory row; legacy rows keep the default workspace so they stay destroyable). A same-name
  create is refused (it would re-share state) — "pick a new name or destroy it first".
- **Direction 2 ("destroy started provisioning") = routing gap + destroy-flow design:** (a) new
  MIRROR guard in `intent_guard` — an explicitly destructive message misclassified as `create` is
  redirected to the destroy flow, never provisioning; (b) destroy no longer runs the create-style
  param collection at all: `_destroy_resource` resolves its target from the INVENTORY (exact name /
  unambiguous typed ref; fuzzy+multiple ⇒ ask; not-inventoried ⇒ honest refusal; bulk ⇒ refused),
  confirms via the approval gate, and tears down that resource's OWN state workspace.
- **Action-vs-operation HARD GUARD** (`agents/plan_guard.py`, pure): after every `show_plan`, the
  plan must match the classified action — create ⇒ zero delete/replace; modify ⇒ update-in-place
  only; destroy ⇒ deletes only; read ⇒ no plan at all. Violations block BEFORE the approval gate
  with an explanation. Wired into create/modify/destroy paths.
- _Proven: 60+ unit/integration tests (A1–A6 incl. a 25-phrasing read sweep and both swap
  directions); state isolation on real terraform (`demo-null`); **live on AWS** (pytest tier:
  create X → create Y with zero-destroy plan → both exist → destroy X → only X gone) and **live
  through the product**: two buckets created via chat+approval in one session, destroy-by-name
  removed exactly the named one (+0/-4 plan), the sibling survived._

### N-03 — conversational memory (CRITICAL)
Root cause: every agent called Gemini with ONLY the current message (`thread_id` is per-run; the
transcript sat unused in `messages`). New `agents/memory.py` (deterministic, DB-backed): full
transcript for short threads; recent-window + older-user-turn digest within a char budget for long
ones (early facts survive); threaded into `general`, `knowledge`, and the router's classification
context (reference resolution). _Live: the exact screenshot-16 prompt now answers "Your previous
question was: '…'" verbatim; E2E journey (3 turns + "what have I asked?") green; 5 integration
tests incl. a 40-turn budget test._

### N-02 — usable VMs + in-product credential delivery
`allowed_cidr` is now a decision-critical param on aws.ec2/azure.vm/gcp.vm (validated CIDR/bare-IP,
`none` = explicitly closed); modules open SSH 22 / RDP 3389 to THAT CIDR only (never 0.0.0.0/0 for
admin). One-time reveal: `POST /runs/{id}/credentials` — whitelisted to the run's real sensitive
outputs, Redis NX one-shot (second attempt 410), read via raw `terraform output -raw` from the
run's state workspace, never logged/persisted; chat success card gets a **Reveal credential**
button with copy/download. _Reviewer catch: the GCP instance had no network tags, so its day-2
firewall rules never attached — fixed._

### N-01 — verification hang
Root cause was FRONTEND: `approveRun` never cleared the message's `streaming` flag (sendText's
finally deliberately skips interrupted messages), so `isLive` kept the LiveTimeline's spinner on
"Verification" forever on apply runs (plan-only runs completed — exactly screenshots 4/5/19 vs 15).
Fixed: `done` handling + defensive finally in the store. Backend hardened to the same contract:
`verify()` is timeout-bounded (30s) with real SDK reconciliation (EC2 describe / S3 head) and warns
instead of hanging; failed runs persist as failed. _Live: both apply journeys reached `done` with
green verification checks._

### N-06 / N-05 / N-04 / N-07
- **N-06:** `agents/cards.py` — per-type success cards (VM→host/user/port/key-reveal;
  S3→name/ARN/region/console; VPC→id/CIDR; DB→endpoint/secret-ref; generic fallback) emitted by
  `verify()` as the run's answer. _Live: S3 card posted in chat with real ARN._
- **N-05:** azure-vm module now genuinely supports **Windows Server 2022** (separate
  `azurerm_windows_virtual_machine`, generated `random_password` as a sensitive output, RDP) plus
  Ubuntu 22.04/24.04/Debian 12; B/D/E-series sizes; default-RG semantics kept. `terraform validate`
  clean. _Live: a real Azure plan for windows-2022 + Standard_D2s_v5 → +9 resources incl. the
  Windows VM + NSG (apply still gated by the sandbox SP's missing Contributor — known)._
- **N-04:** assistant messages render full markdown (react-markdown + GFM, code blocks with copy,
  links/tables/lists, design tokens); user messages stay plain text (no injection surface). 5 RTL
  tests.
- **N-07:** the timeline Finalize node shows a short status (≤140 chars), never a verbatim copy of
  a long answer; read-path answers already concise/structured.

### Environment defects found by the pass (fixed)
- Root-run tests created `terraform.tfstate.d/` root-owned on the bind mount → the API container
  (non-root) couldn't create state workspaces. Fixed live + the `api-test` service now restores
  world-writable perms after every run (exit code preserved).
- The api image's baked `alembic/` is stale (only `app/` is mounted) — migrations must run via
  `api-test` (full backend mount): `docker compose --profile test run --rm api-test sh -lc
  "alembic upgrade head"`.

---

## Observability + SSO fix pass — 2026-07-06
Two independent "wired but broken" defects, both root-caused from the live stack before fixing.

### Langfuse: dashboard showed 0 traces / $0.00 / 0 tokens (M6 claimed done)
**Root causes (two, compounding):**
1. **Wrong project keys.** The `.env` `LANGFUSE_PUBLIC_KEY` (`pk-lf-c4fa71a6…`) belonged to a
   different project ("myproject") in the same instance — 272 traces had been ingested THERE
   while the dashboard watched the `aegisops` project. Not flush, not network: the compose api
   env already pointed at `http://langfuse:3000` and `runner.py` flushed in `finally`.
2. **Instrumentation too shallow.** The `observations` table had **0 rows ever** — `runner.py`
   posted one flat trace per run and nothing else (no spans, no generations ⇒ no tokens/cost
   even in the wrong project).

**Fixes:**
- `.env` keys switched to the compose-provisioned `aegisops`-project keys (`pk/sk-lf-aegisops-local`);
  `.env.example` + compose comments now state the keys MUST belong to the `aegisops` project.
- Full span-tree instrumentation (`integrations/langfuse_client.py` rewritten):
  * **One trace per run, trace id == run id** — the approval resume re-attaches to the SAME
    trace; trace ↔ context-graph ↔ run are one id (graph already stored `trace_id`).
  * **A span per graph node/sub-step** driven by `agents/timing.py` (zero per-agent changes):
    deterministic span ids (`<run_id>:<step>`) let the resume close the **approval span across
    the human interrupt** with the original start — the true wall-clock wait is on the span.
  * **LLM generations** recorded at the Gemini chokepoints (`agenerate` + `stream_answer`,
    including per-retry ERROR generations and truncation): model, prompt/response (redacted),
    token usage from `usage_metadata`, latency, and **USD cost** computed from
    `GEMINI_COST_PER_1M_INPUT/OUTPUT` (self-hosted Langfuse has no Gemini price table).
  * **Tool spans** for terraform init/plan/apply/destroy, ServiceNow post/patch/get, RAG
    retrieve, and the cloud availability SDK checks — inputs redacted, failures recorded ON
    the span (level=ERROR + message) and re-raised, nested under their calling step (A→B→C).
  * Trace name/tags/metadata: `<domain>-run`, user, session id, context id, agent/cloud/env/
    intent tags; **all payloads pass the existing redaction layer** — secrets never leave.
- _Live-verified (real requests, project `aegisops`):_ domain-named traces (`general-run`,
  `knowledge-run`, `devops-run`, `cloudops-run`) each carrying the nested node tree
  (router → agent → … → finalize → servicenow_update → notify), `rag.retrieve` tool spans,
  and `gemini.generate`/`gemini.stream` generations with **real token counts and USD costs**
  (e.g. 1215 prompt + 68 completion ⇒ $0.0005345) — the dashboard is no longer $0.00/0 tokens.
  The error path was also live-verified: during a window where the Gemini key was rejected,
  every retry appeared as an **ERROR generation on the trace** (status captured on the span,
  not swallowed), and the trace still closed with the run's failure output.
- **Tests:** `tests/test_langfuse_tracing.py` (18 incl. a live API round-trip that reads the
  tree back via the Langfuse public API): trace-id identity across resume, span nesting,
  approval-span-across-interrupt, error-on-span, usage+cost math, secret-never-survives,
  disabled-noop. `test_stream_resilience.py`'s fake updated to the raw-chunk interface
  `stream_answer` now consumes (it reads token usage off the final chunk).

### Keycloak SSO: "Continue with Keycloak SSO" failed at the first redirect
**Root causes (two layers, both the browser-vs-container host split):**
1. `/auth/sso/login` built the authorization URL from discovery fetched over the container
   network → `302 Location: http://keycloak:8080/…` — a docker service name the browser can't
   resolve. (Redirect URIs, client config, PKCE S256, realm import all verified correct.)
2. Once the browser leg worked, `/auth/callback` 500'd with `Invalid issuer`: Keycloak derives
   a token's `iss` from the URL the auth request came through, so SSO tokens carry
   `http://localhost:8080/realms/aegisops` while validation pinned the internal
   `http://keycloak:8080/…` (which password-grant tokens carry).
**Fixes:** new `KEYCLOAK_PUBLIC_URL` setting (compose api env defaults it to
`http://localhost:8080`; empty ⇒ falls back to `KEYCLOAK_URL` for host-run dev);
`build_auth_url` rewrites only the ORIGIN of the discovery authorization endpoint to the
browser-facing host (token exchange + JWKS stay internal); `validate()` accepts exactly the
two known realm issuer URLs (internal + browser-facing), nothing else.
- _Live-verified (scripted browser-equivalent round-trip + real browser):_ SSO login →
  Keycloak form → callback → session cookie → `/auth/me` 200 (`maya.okafor`,
  `can_approve: true` — RBAC intact) → logout → 401 → fresh SSO login works again; the
  password-grant form login unregressed.
- **Tests:** `tests/test_auth_sso.py` (browser-host rewrite, PKCE params, dual-issuer accept/
  reject, fallback, plus a host-gated live round-trip via `AEGISOPS_TEST_SSO_LIVE=1`);
  Playwright `e2e/sso.spec.ts` — real-browser SSO round-trip, passing (runs unauthenticated,
  one extra grant per suite run so the login rate limiter is not stampeded).

---

## A. Foundation & ops
- [x] `docker compose up -d` starts PG+pgvector, Redis, Neo4j, Keycloak, Langfuse(v2), OTel
      Collector, Prometheus, Grafana — pinned versions + healthchecks. _(verify with `make up`)_
- [x] `make migrate` runs Alembic (15 tables, pgvector + HNSW index) + Neo4j schema/constraints;
      `make seed` loads real data (org, 8 roles, users, integrations, 4 Knowledge docs → chunks,
      notifications) — idempotent. _(verified live)_
- [x] `make dev` starts backend (uvicorn :8000) + frontend (next :3000); `/healthz` + `/readyz`
      check all deps.
- [x] `/metrics` exposes Prometheus metrics; Grafana dashboard provisioned.
- [x] No grep hits for TODO/FIXME/mock/placeholder/NotImplemented in app code.

## B. Auth & RBAC (Keycloak, real)
- [x] Keycloak realm (8 roles, frontend+backend clients, seed users) imported on boot.
- [x] Login screen matches source; real OIDC — password grant (form) + Auth Code + PKCE (SSO);
      callback exchanges code → session. _(verified live)_
- [x] JWT validated via JWKS on every API call; unauth → 401; UI gated behind login.
- [~] Role capabilities enforced (approver/initiator/read-only deps verified live); per-route +
      per-tool guards applied as side-effecting endpoints/tools land in M3–M5.
- [x] Sign-out ends session (revoke refresh + clear cookie). _(verified live)_

## C. UI parity (pixel-exact vs source HTML, dark+light+mobile)
- [x] Tokens/fonts/animations/responsive copied verbatim into `globals.css`.
- [x] Sidebar + navs with correct active styling. _(verified)_
- [x] Top-nav selectors (cloud/model/theme/notifications/profile + role switch) work; one menu
      open at a time. _(org/env/region exist in state but are not rendered in the design's
      top-nav — the source HTML is authoritative; role lives in the profile menu.)_
- [x] Theme dark/light/system incl. live OS follow (matchMedia); cycle order correct. _(verified)_
- [x] Command palette (⌘K) opens/closes; actions + nav run. _(verified)_
- [n/a] Overview summary cards — present in logic.js state but not rendered in the source design.
- [x] Mobile drawers (sidebar overlay) behave per source. _(verified at 390px)_

## D. Chat workspace (real Gemini via SSE)
- [ ] Composer behavior; live thinking-timeline; real Gemini token stream.
- [ ] Two-tab message view (Conversation + Analysis/References).
- [ ] Confidentiality badge from real classifier.
- [ ] Interpreted intent + workflow + plan/input JSON.
- [ ] Feedback persisted + linked to context graph.
- [ ] Follow-ups keep context; SSE reconnect; secrets masked.

## E. Artifact panel (8 tabs, real run data)
- [ ] Timeline · Reasoning · Terraform · Logs · Metrics · Traces · References · Approvals.

## F. Approval & execution modes (real, HITL)
- [ ] Interrupt at approval; RBAC-gated; modes dry_run/plan/apply/destroy.
- [ ] Approve → real apply/destroy → state update → verify; reject halts.
- [ ] Resumable after restart; idempotent; immutable approval record.

## G. CloudOps (real end-to-end)
- [x] **LIVE-VERIFIED with real Gemini + AWS**: "create a t3.micro EC2" → Router→CloudOps (100%) →
      aws.ec2 template → AWS availability check → real `terraform plan` (+1) → approval interrupt →
      `terraform apply` created instance `i-090d9b12107402936`; then a destroy run terminated it
      (`1 destroyed`) with clean verify+finalize. Approvals immutable; context graph written.
      (Fixes during this run: AWS `AWS_SESSION_TOKEN` support for sandbox creds; Neo4j map-property
      JSON encoding; non-fatal graph writes.)

## H. DevOps (real)
- [ ] Staged state machine via GitHub + K8s; approvals; repo link in chat.

## I. SRE (real)
- [ ] Triage; telemetry; RAG runbooks; decision matrix; approval-gated remediation.

## J. Modules (real, org-scoped)
- [ ] All 7 modules from real data; integrations grid live health; responsive grids.

## K. Knowledge / RAG (real)
- [~] Ingest chunks + stores docs in pgvector (embeddings generated once a Gemini key is set);
      semantic (cosine) search + trigram keyword fallback + retriever built. _(4 docs seeded;
      citations wired into Analysis/References UI in M5)_

## L. Context graph (real, Neo4j)
- [~] Full node/relationship model + schema/constraints + redaction + resume API built. _(writes
      exercised per-run in M4; immutability enforced on close.)_

## Workspace wiring & modules (M5) — live-verified
- [x] Composer → POST /chat real SSE: step/token/analysis/reference/confidentiality/interrupt/
      done/error consumed live (fetch-based SSE client, POST). Simulated stream removed.
- [x] Two-tab Conversation / Analysis-References per AI message; confidentiality badge from real
      classifier; feedback → POST /feedback (optimistic); follow-ups keep session context.
- [x] Approval gate → POST /approvals/{runId} (RBAC approver) streams the continuation.
- [x] Artifact panel 8 tabs fetch GET /runs/{id}/{tab} (timeline/reasoning/terraform/logs/
      metrics/traces/references/approvals) — real run data + empty states.
- [x] Modules bound to GET /modules/{name} (real org-scoped counts/rows); Admin integrations grid
      → GET /integrations live health; TopNav bell → GET /notifications.
- [~] Full streamed Gemini answer + CloudOps plan/approval render: wired; shows live error until
      GEMINI_API_KEY is set, then renders end-to-end.

## Agents & SSE (M4) — backend live-verified
- [x] Real LangGraph graph: router → cloudops/devops/sre/knowledge/general + approval(interrupt) →
      execute → verify → finalize → servicenow → notify. Compiles + runs.
- [x] Durable Postgres checkpointer (interrupt/resume/restart-safe); checkpoint tables created.
- [x] SSE contract: step/token/analysis/reference/confidentiality/console/interrupt/done/error —
      streamed end-to-end via POST /chat; /approvals, /chat/stream (Last-Event-ID), /runs, /runs/input.
- [x] **CloudOps multi-cloud**: template registry (aws s3/vpc/eks/rds/ec2, azure storage/rg, gcp gcs,
      generic module) → Pydantic validate → availability (SDK reads) → terraform plan → approval →
      apply/destroy → verify. Cloud SDKs read-only; Terraform mutates; approval gate enforced.
- [x] **DevOps**: staged GitHub→CI→image→K8s pipeline with approval gate.
- [x] **SRE**: triage → telemetry (Prometheus) → RAG runbooks → decision matrix → gated remediation.
- [x] Router creates ServiceNow ticket + opens context graph; approval recorded (DB + graph, immutable).
- [x] Confidentiality classifier on responses; redaction on console; idempotency on tool exec.
- [~] Full Gemini reasoning + token streaming + CloudOps apply/approval live run: wired + checkpointer
      ready + terraform plan proven; **activates when GEMINI_API_KEY (and cloud creds) are set**.

## Integration clients (M3)
- [x] All built real + import-clean: Gemini, ServiceNow, GitHub, AWS/Azure/GCP/VMware readers,
      Kubernetes, Prometheus, TerraformRunner (init/plan verified), AnsibleRunner, console.
- [x] `GET /integrations` live health (datastores + observability live; cloud/SNOW/GitHub/Gemini
      "not configured" until creds added). Security: redaction, idempotency, confidentiality.

## M. Observability (real)
- [x] Langfuse trace + OTel span per run (linked to context id); per-node records in context graph
      + SSE steps. Prometheus `aegisops_*` metrics at /metrics tagged by agent/workflow/domain/env;
      Grafana dashboard provisioned. Structured JSON logs (structlog) with correlation ids; secrets
      redacted, never logged.

## N. Non-functional
- [x] CORS locked; per-IP rate limiting (SlowAPIMiddleware); graceful shutdown; stateless API
      (state in PG/Redis/Neo4j); idempotency keys on tool exec; durable checkpoint resume; resilient
      degraded startup with /readyz truth.

## O. Tests (real, green) — Phase 6: 210 → Phase 7: 284 → Phase 8: 377 → **2026-07-06: 394 backend / 25 vitest / 13 e2e** (adds Langfuse tracing + SSO suites, incl. a real-browser SSO round-trip)
- [x] Phase 8 adds the full safety-invariant matrix (A1–A6 incl. live-cloud tier with teardown),
      memory/continuity, run-lifecycle, usable-outputs, provider-accuracy, and markdown-rendering
      suites per `Screenshots/03_TEST_MATRIX.md`.
- [x] Backend pytest **284 passing** (Phase 7 adds ~74: intent-guard safety incl. the exact screenshot
      prompts, per-cloud machine shapes, broad/typed inventory recall, provider-error signatures,
      stream-truncation resilience, plan-parse integrity) — run **in-container** (`make test` → `api-test`) against
      real PG/Redis/Neo4j + live cloud creds: routing/cloud-resolution scenarios (~55 parametrized cases),
      per-module param collection + schema validation (all 14 templates), SSE event contract +
      reconnect/exactly-once, approval/route decision edges, RBAC at side-effecting endpoints, secret
      redaction, idempotency (real Redis), pending multi-turn collection (real Redis), inventory
      persistence + reference resolution (real Postgres), cloud-SDK import guards, confidentiality,
      health/metrics/auth-boundary.
- [x] Frontend Vitest + RTL **20 passing** (was 4): store message↔run binding, streaming render,
      artifact-panel-per-message, history restore, feedback, confidentiality badge + param card (RTL),
      SSE CRLF frame parsing.
- [x] Playwright E2E **9 passing** (chromium + Pixel 7 mobile): real Keycloak login → workspace → real
      streamed run → per-message artifact panel → theme + mobile. Auth reused via storageState.
- [~] Full apply→day-2 browser journey + a kill-mid-interrupt restart-resume test remain manual/
      prior-phase-verified (see Phase 6 known gaps).

## P. Delivery
- [x] Generated `README.md` (fresh-clone runbook); `.env.example` lists every variable; `.env`
      gitignored; no secret committed. No grep hits for TODO/FIXME/mock/placeholder/NotImplemented
      in app code.

## Q. Production remediation (Stage A/B — 2026-07-11 →)

> The ground-truth analysis (`ANALYSIS.md`) found that several claims above are **not** met by the
> code as it stands: multi-tenancy is a single default org (P2), credential reveal and read/stream
> endpoints are under-authorized (P1/P3), the API is not horizontally scalable (in-process SSE
> channels, P4), policy checks are hardcoded `True` (P8), the Traces tab is static (P9), and SRE
> remediation reports success without acting (P7). Where this file and the code disagree, the code
> wins. Remediation is governed by the amended plan.

- [x] **Stage A (docs)** — plan amended per the owner's production directive +
      `AEGISOPS_TARGET_ARCHITECTURE.md`: decisions 7–13 locked in `FIX.md`; Split-Trust +
      Governed Executive Loop folded into `docs/fix/01_harness.md`; Context Engine (5 layers,
      incl. context offloading M5) into `docs/fix/03…`; U6 rewritten + D3 resolved (INVEST) in
      `docs/fix/04…`; `docs/fix/05…` reconciled to the authoritative architecture;
      `docs/fix/07_roadmap.md` re-emitted as Phases 1–3 with per-item acceptance tests;
      execution checklist appended to `FIX.md §8`. **Awaiting owner review before Stage B.**
- [x] **Phase 1 — Trustworthy** — ALL 18 items implemented, tested, committed one-per-commit
      (S0 S1 S2 S3 S4 S5 · A1+B7 A2 A4 A5 · B5 B6 · U4 · honesty labels · O2 C1 D1 D4).
      **Awaiting owner sign-off at the exit gate before Phase 2.**
  - **Exit-gate evidence (2026-07-12):**
    - Full regression green: **backend pytest 438 passed / 2 skipped**, **frontend vitest 28
      passed**, **tsc clean**. (+63 backend tests over the pre-Phase-1 375.)
    - **Two orgs isolated in API + UI** — live walkthrough vs the running API + real Keycloak
      (realm recreated to import the org groups/mapper + org-B users): maya (northwind-financial)
      and bob.chen (acme-industrial) resolve to distinct org_ids from the Keycloak `org` claim;
      bob cannot see/rename/read/continue org-A's session (list excludes it; PATCH/GET/`/chat`
      → 404); `/overview` is per-org. `test_tenancy.py` (11) covers this at the API level.
    - **Read-only cannot initiate** — audit.viewer `POST /chat` → 403 live; composer shows a
      read-only notice. `test_rbac_endpoints.py::test_chat_requires_initiator`.
    - **Initiator cannot self-approve a prod run** — `test_tenancy.py::test_four_eyes_blocks_prod_self_approval` (403).
    - **Concurrent double-approve → exactly one apply** — `test_idempotency.py` (node aborts,
      apply() proven unreachable) + `test_tenancy.py::test_double_approval_endpoint_guard` (409).
    - **Reveal gated + audited** — no fresh step-up proof → 401; cross-org/non-owner → 404;
      value once then 410; **every attempt writes an audit row (value never logged)** — verified
      live in `audit_log`. `test_tenancy.py::TestCredentialRevealS1`.
    - **No dishonest surface** — policy "not evaluated", SRE "proposed, not executed", Traces
      deep-link (no fake spans). `test_honesty_labels.py`, `test_templates.py`.
  - **Two honest caveats for the owner (see below):** (1) the branch carries a large pre-existing
    CloudOps-V1 WIP baseline that these commits build on — some depend on untracked files
    (`plan_guard.py`, `memory.py`, `cards.py`, migration `0003`); a baseline commit of that WIP is
    needed for the Phase-1 commits to stand alone. (2) Browser **Playwright e2e for the NEW
    Phase-1 flows** (two-org login isolation, read-only composer, reveal step-up modal) is not yet
    written — the existing 9 e2e cover the core streamed-run flow; the new flows are proven at the
    API/integration/unit level and via the live walkthrough, but the §5 Playwright coverage for
    flows 1–3 remains to add. **→ CLOSED as the first Phase-2 item (see below).**

## R. Phase 2 — Production harness + Context Engine (started 2026-07-12)

- [~] **Phase 2 in progress.** Checklist order per FIX.md §8; one item at a time, acceptance test,
      suite green, commit. Exit gate: worker-kill mid-apply recovers exactly once; multi-worker
      streaming; **turn-20-of-100 recall in the UI**; real failed policy check; real Traces tab;
      honest model menu; **warm-turn latency ≤15s measured before/after (LAT)**.
  - [x] **E2E (owner-ordered, before B1)** — Playwright e2e for §5 flows 1–3 + full browser
        suite re-run (2026-07-12): new `frontend/e2e/tenancy_roles_reveal.spec.ts` (6 tests):
        flow 1 org-A sees Northwind / org-B sees Acme only (never Northwind); flow 2 read-only
        shows the notice + no composer, initiator sees the composer; flow 3 Reveal → step-up
        modal → wrong password stays with a re-auth message → correct password shows the value
        once (SSE + credential responses mocked so the real modal runs without cloud creds; the
        backend reveal contract is covered by `test_tenancy.py::TestCredentialRevealS1`).
        **Full browser suite: 22 passed / 2 skipped (by-design mobile) / exit 0.** One live-LLM
        streamed-run test is flaky-but-passes in this env (invalid Gemini key → slow backend
        retries occasionally exceed the 30s UI wait; clean with a real key). Frontend served by
        host `next dev` for live code (the compose frontend image was 9h stale).
  - [x] **B1 Redis Streams event bus** (2026-07-12): new `RedisChannel` (XADD on emit, background
        XREAD-BLOCK pump feeding the same `.queue` the memory consumer drains) behind the unchanged
        `Emitter`/`_sse` contract; flag `AEGISOPS_EVENT_BUS=memory|redis` (default memory =
        rollback). Terminal runs publish an EOS marker (never delivered to the client) + set a TTL
        so the stream self-evicts — fixing the unbounded `_channels` leak (P4) and enabling
        worker-agnostic streaming/reconnect. The memory path is byte-identical (test_sse_contract
        unchanged). Evidence: `test_event_bus_redis.py` (5, incl. multi-worker publish-A/consume-B
        + TTL-on-terminal) + `test_sse_contract.py` (7) green.
  - [x] **B2 RunSupervisor** (2026-07-12): both `_drive` sites now run via
        `get_supervisor().run(run_id, drive)` — a tracked task + a per-run Redis heartbeat
        (`run:<id>:hb`, TTL 45s / refresh 15s) instead of fire-and-forget `create_task`.
        `is_live(run_id)` answers reconnect/liveness; an expired heartbeat marks a crashed worker's
        run for the B3 reconciler; `main.py` lifespan calls `drain()` on shutdown to cancel
        in-flight runs and persist them `failed`. Evidence: `test_supervisor.py` (2).
  - [x] **B3 stranded-run reconciler** (2026-07-12): periodic sweep (started in the lifespan) over
        `runs.status IN (running, applying)`; a run that is neither locally-live nor
        heartbeat-alive is stranded → resumed from the LangGraph checkpoint if `aget_state().next`
        is set (re-driven via the supervisor, so A1 idempotency guards it), else marked failed
        honestly ("recovered after an interruption — nothing changed beyond the Logs").
        `awaiting_approval` runs are left for the human. Evidence: `test_reconciler.py` (5), incl.
        the kill-mid-apply case (recovered to terminal once, in-flight claim untouched — no re-apply).
    - **Hang investigation + defect fix (before committing B3).** The first "full suite after B3"
      run hung (0 bytes of pytest output, container up 29 min). Root cause from the evidence:
      **0 bytes = pytest never printed a single dot**, so the stall was BEFORE pytest — the
      `pip install` step in the api-test command (a PyPI/network stall), not test code; an
      identical-code re-run progressed to completion. SEPARATELY, a genuine defect was found and
      fixed regardless of the hang: **B3's reconciler auto-started a periodic sweep loop in EVERY
      `TestClient` lifespan** (the `client` fixture), and a pre-gate suite run (reconciler on)
      stalled near 100% — consistent with leaked/accumulated sweep loops pressuring teardown / the
      DB pool. Fix: gate the reconciler behind `AEGISOPS_RECONCILER=on|off` (set `off` in the
      api-test service, so no background loop starts under pytest — tests drive `sweep()`
      explicitly), and make `Reconciler.start()` idempotent (never accumulate loops). Verification:
      the full suite now completes cleanly **twice in a row** with the gate — run #1 **450 passed /
      2 skipped / PYTEST_EXIT=0**, run #2 **450 passed / 2 skipped / PYTEST_EXIT=0** (direct exit
      code, full output, no tail-masking).
  - [x] **B4 verify cross-cloud + honest cards (C2)** (2026-07-12): `_reconcile_checks` now does a
        real live check for Azure VMs (Azure Compute `list_vms`) and GCP VMs (`list_all_instances`),
        matched by the resource's stable name — a missing/terminated resource is a real FAILED
        check, never "outputs present" theater. Both readers thread-offload; the whole reconcile is
        30s-bounded by `verify()`, so a slow cloud warns rather than hangs (N-01, per cloud). C2:
        the success card's host/connection derive from generic outputs (public_ip/login_user) that
        Azure/GCP VMs also emit. Evidence: `test_verify_cross_cloud.py` (5, incl. Azure slow-SDK →
        bounded warn).
  - [x] **A3 unique plan-file per run + remote-backend plumbing** (2026-07-12): every runner now
        takes a `run_id`, so the saved plan-file is `aegisops-<workspace>-<run_id>.tfplan` — two
        operations (even two creates of one resource, or two concurrent runs in a module dir) never
        share/overwrite a plan file; plan and apply reuse the same path via the same run_id. `init`
        supplies an S3+DynamoDB `-backend-config` (state key namespaced per module+workspace,
        DynamoDB lock) when `AEGISOPS_TF_BACKEND=remote`; local stays the dev default. Evidence:
        `test_terraform_backend.py` (6). **PENDING (infra):** remote apply is untestable in this
        env — no S3 bucket / DynamoDB table, and the module backend blocks are `local` (switching
        to `s3` is the documented migration). Plumbing + flag are in; remote apply awaits a bucket.
  - [x] **LAT latency pass** (2026-07-12): `init` skips the full `terraform init` when the module
        is already initialized (`.terraform/` + lockfile), and `TF_PLUGIN_CACHE_DIR` (a `tfplugins`
        named volume in compose) reuses downloaded providers across modules. **Measured init
        before/after on demo-null: cold 3.97s → warm 0.002s (skipped) = −3.97s per warm turn.**
        Real cloud providers are far larger (the prior audit measured ~19s cold init for EC2), so
        the skip removes ~19s from a warm cloud turn; the plugin cache makes even the cold init
        cheaper. Escape hatch: `AEGISOPS_TF_SKIP_INIT_WHEN_READY=false` / `init(force=True)`. The
        warm-skip also sidesteps the residual-backend re-init prompt that fails a cold re-init.
        **PENDING (creds):** the full warm-turn ≤15s target (init≈0 + cloud `plan` ~21s + LLM
        classify/extract) needs a creds-enabled provisioning turn to confirm end-to-end — the init
        component is measured; the cloud-`plan` + LLM components are not runnable here (invalid
        Gemini key, no cloud grant). Evidence: `test_terraform_latency.py` (5). **Skip-safety:**
        `TestStateIsolation` caught that a module can claim initialized (.terraform/ + lockfile)
        yet be unusable (provider cache evicted → dangling plugin links); fixed so the warm path
        falls back to a full init on any such mismatch rather than failing the run.
  - [x] **M1/M2/M3 Context Engine core** (2026-07-12): `memory.build_context(session, purpose=,
        budget=, current_message=)` threaded into the router (M3, replacing the fixed last-8
        window), general, and knowledge — returns a relevant-earlier-turns slot + the transcript.
        **M2 positional recall**: `get_turn(session, N, role)` returns the Nth turn verbatim;
        `detect_recall` parses "what was my 20th question?" etc.; the general agent answers exact
        positional recall **deterministically from the store — no LLM guess** (works even if the
        LLM is down, can't hallucinate a different turn). **M2 semantic recall**: `retrieve()` uses
        pgvector over per-message embeddings (migration `0006`, `messages.embedding` + HNSW),
        embed-on-write in `api/chat.py` (best-effort; NULL without a Gemini key → pg_trgm keyword
        fallback). Headline test green: 100-message session → turn 20 returned verbatim. Evidence:
        `test_memory.py` (+6). **PENDING (Gemini key):** the full turn-20-recall UI demo needs a
        valid Gemini key (the router + general LLM calls) — the recall logic itself is deterministic
        and proven; the exact-recall answer is even LLM-free once routed.
  - [x] **M5 context offloading** (2026-07-12): large operational payloads (plan JSON, apply logs)
        live in the store (`runs.plan_json`), never inlined into the transcript/LLM prompt;
        `memory.plan_ref_line` is the short reference the context carries and `memory.fetch_plan`
        fetches the full plan on demand. Evidence: `test_memory.py` (2) — a 30-plan session stays
        within the purpose budget with no raw plan JSON inlined; fetch returns the stored plan.
  - [x] **U1 real policy checks** (2026-07-12): each `_*_policy` now evaluates genuine predicates
        over the planned resource attributes (`terraform show -json` `change.after`, stashed
        in-memory by `show_plan` via `runner.planned_resources()` — never persisted). EC2 root-volume
        encryption + IMDSv2, S3 public-access-block + SSE + versioning, RDS storage_encrypted +
        publicly_accessible, Azure storage min_tls_version + public-blob, GCS uniform-access +
        force_destroy are real pass/fail; a plan with a control DISABLED renders a genuine FAILED
        check. Controls not extractable from the plan stay honest "not evaluated" (P8 label).
        Evidence: `test_policy_real.py` (7) + `test_templates.py` (7) green.
  - [x] **DEF defaults honesty** (2026-07-12): `_defaulted_dependencies` computes the
        silently-defaulted dependency placements (AWS EC2 → account default VPC/subnet, GCP VM →
        project default network, Azure VM → auto-created `<name>-rg`) and adds them to the plan
        JSON + interrupt payload; the approval card renders an amber "Defaults applied" section so
        there is no invisible placement. The resolved subnet id is named when the plan reveals it.
        Evidence: `test_defaults_honesty.py` (6); vitest 28; tsc clean.
  - [x] **U2 SRE real signals + K8s actions** (2026-07-12): `recent_deploy` is now a real
        Prometheus query (deployment-generation change in 15m via kube-state-metrics), defaulting
        False when Prometheus is unavailable — the old hardcoded `recent_deploy=True` is gone; cpu
        saturation + pod restarts are real queries too. `tools/kubernetes.py` gains real
        `restart_deployment` (rollout-restart annotation), `scale_deployment` (patch replicas), and
        `rollback_deployment` (patch to the prior ReplicaSet's template); `sre_execute` dispatches
        the approved action to them when a cluster is configured (→ `applied:True` with the real
        result), reports `remediation_failed` truthfully on error, and stays "proposed, not
        executed" when no cluster / for `investigate`. Evidence: `test_sre_remediation.py` (8) +
        the P7 honesty test still green.
  - [x] **U3 real LLM provider seam + honest model menu** (2026-07-12): new `integrations/llm/`
        package — `LLMProvider` protocol, `GeminiProvider` (the one provider we ship), and
        `get_provider(settings, model)` that returns the default for `None`, the requested id
        when served, or raises `UnknownModelError` (naming what we serve) — never a silent
        fallback. `body.model` is now honored: `/chat` validates it up front (unknown → clear
        400 before any DB) and binds the resolved id to the run via a **contextvar** in
        `gemini.py` (per-asyncio-task, so concurrent runs don't clobber each other's model —
        the shared GeminiLLM singleton is never mutated); every Gemini call reads it, so
        router/cloudops/devops/sre all honor the choice. `GET /models` exposes the real catalog.
        The frontend menu, which advertised Claude/GPT-4o/Azure/Llama we can't run, is trimmed
        to the 3 real Gemini ids and sends the raw id; store default is now a served id.
        Evidence: `test_llm_provider.py` (9); vitest 28; tsc clean.
  - [x] **O1 real Traces tab** (2026-07-12): the Traces artifact was a Phase-1 honesty stub
        (no spans, "coming soon", Langfuse deep-link only). It's now a real tree built from the
        run's `run_steps`: a run-root span over ordered child spans, each showing the step's
        actual elapsed time (`_fmt_dur`) — no fabricated `—`. In-flight steps (and a running
        run's root) show `···` rather than a made-up number; failed steps are red and carry the
        truncated error; tool/human/retry are annotated. The full nested trace (tokens/cost)
        still deep-links to Langfuse, now shown alongside the tree; a run with no recorded steps
        falls back to the honest note + link. `coming_soon` retired. Evidence:
        `test_traces_tree.py` (4, pure builder — no DB) + updated traces honesty test; vitest 28.
  - [x] **O3 metrics hygiene + SSE rate-limit exemption** (2026-07-12): three declared metrics
        were always-empty series (a lie on the dashboard). Fixed honestly: `AGENT_STEP_DURATION`
        is now observed with the real per-step elapsed in `timing.end_step` (grouped by
        subsystem via `_AGENT_OF`); `APPROVAL_WAIT` is observed in `resolve_approval` with the
        real human wall-clock wait (from the approval step's start to the decision), labeled by
        domain + decision; `TOOL_RETRIES` — which had no real population source in this item's
        scope and no dashboard panel — is removed rather than left empty. Separately, the SSE
        endpoints (`POST /chat`, `GET /chat/stream/{id}`) are marked `@limiter.exempt`: an SSE
        connection is long-lived and reconnects with Last-Event-ID, so counting it against a
        per-minute budget would throttle normal streaming. The limiter moved to a new
        `ratelimit.py` so `main.py` and `api/chat.py` share one instance without an import cycle.
        Evidence: `test_metrics_hygiene.py` (6) incl. a live-DB step-duration sample.
  - [x] **D2 same-txn inventory write + orphan sweeper** (2026-07-12): a successful apply mutates
        real infrastructure that can't be rolled back, so the inventory `Resource` row and the
        run's `outcome` are now written in ONE transaction in `cloudops_execute`, and the outcome
        carries a self-contained `_inventory` recovery payload. If the row write is interrupted,
        the outcome (persisted by the outer driver) still carries the payload, so the new
        reconciler `sweep_orphans()` rebuilds a missing row **from the run alone — no cloud read**.
        New inventory seam: `inventory_payload` (pure), `upsert_resource`/`mark_destroyed_txn`
        (session-scoped, no own txn), `recover_missing` (sweeper recovery, idempotent),
        `record_graph` (graph mirror split out). Evidence: `test_orphan_inventory.py` (4) —
        crash-inject → recovered, idempotent, legacy no-payload left alone. The cloud-level orphan
        (apply done but the DB txn never ran) still needs a live TF backend to reconcile from
        state and is recorded pending (no creds); B3 already brings such a run to a terminal state.
  - [x] **U5 mid-run input — removed (documented choice)** (2026-07-12): the mid-run stdin
        feature was a stub end-to-end — `POST /runs/{id}/input` pushed to `runinput:{id}` which
        **no consumer read**, `CommandConsole.send_input` had **no caller**, `stdin_data` was
        **never passed**, and no frontend UI invoked it. Wiring it would mean a real interactive
        tool path we don't have (terraform/ansible run non-interactively; the human-in-the-loop
        is the approval gate, not stdin). Per the no-stubs rule, removed the endpoint, `send_input`,
        and `stdin_data` (console now opens `stdin=DEVNULL`, which also prevents a command hanging
        on an unexpected prompt). Zero references remain. Evidence: `test_rbac_endpoints.py` —
        `/input` → 404, `CommandConsole` has no `send_input`.
  - [x] **U8 SSE contract regression on the Redis bus** (2026-07-12): proved the Redis Streams
        bus (B1) preserves the SSE contract exactly, so the frontend reducer needs no change to
        run on it. New parity test drives the **full** event vocabulary
        (run/step/token/analysis/params/confidentiality/console/interrupt/error/done) through both
        the in-memory `RunChannel` and the `RedisChannel` and asserts **byte-identical JSON
        frames** (JSON is what the client receives in both modes; the memory path is normalized
        through JSON for an apples-to-apples compare). The reducer + parser are untouched —
        frontend **vitest 28 green**; Playwright **`core-flow` 5/5 green** against the running
        stack, including "sending a message streams a live run into the per-message timeline".
        The live multi-worker-on-Redis-bus streaming + worker-kill recovery is reserved for the
        Phase-2 exit-gate demonstration (as specified).
  - [x] **P16 DevOps CI poll-to-completion** (2026-07-12): the pipeline dispatched the CI
        workflow and then read the *latest* run status once — which could be a stale prior run or
        the just-dispatched run still queued, and `workflow_dispatch` returns no run id to track.
        Now the agent IDENTIFIES the run it created (`find_dispatched_run`: newest
        `workflow_dispatch` run on the branch created ≥ dispatch time, tz-normalized) and POLLS
        **that run id** to completion (`poll_run_to_completion`, bounded by a timeout, streaming
        per-poll progress to the console). The real conclusion drives the outcome: `failure` fails
        the pipeline; a run not yet visible is reported as "dispatched" (not faked); a timeout
        leaves the status non-completed rather than inventing success. Removed the now-unused
        `latest_run_status`. Evidence: `test_devops_ci_poll.py` (5). A live poll against real
        GitHub Actions needs a `GITHUB_TOKEN` (not configured) — recorded pending.
  - [x] **DEF: reconciler redrive never persisted its result** (2026-07-12, found live at the
        Phase-2 gate worker-kill demo): after SIGKILLing worker A mid-run, worker B's reconciler
        logged `reconciler.resumed` — and 60 seconds later `reconciler.marked_failed` for the
        SAME run. Root cause: `Reconciler._redrive` called `run_graph` (which only executes the
        graph) and never persisted status/answer — persistence lived only in the API driver — so
        even a successful redrive left the run `running`, and the next sweep honestly-but-wrongly
        force-failed it; a redriven APPLY would have had its `applied` outcome overwritten with
        `failed`. Fix: the redrive drive now mirrors the API driver — `_persist_result` (status
        `completed`/`failed`/`awaiting_approval` + assistant message), `done` event on the
        channel, `_force_terminal` (B5) backstop on exception. Evidence:
        `test_redrive_persists_the_result_and_second_sweep_is_a_noop` (redrive persists; second
        sweep is a no-op) + the live re-demo showing exactly ONE reconciler action.
  - [x] **M2 amendment: "turn N" recall shape** (2026-07-12, found at the gate): the natural
        "what did I say in **turn 20**?" phrasing matched neither noun-last form in `_RECALL_RE`
        (it knew "my 20th question" / "the first message" but not noun-first "turn 20"). Added
        the `turn N` / `message #N` alternative — deliberately NOT `request N`/`question N`,
        which would false-positive on ordinary sentences ("I request 3 VMs", "question 5 of the
        quiz"). Evidence: `test_detect_recall_parses_turn_n_shape` (+ false-positive guards);
        all 14 memory tests green.
  - [x] **S0 multi-tenancy** (2026-07-11): principal→(org_id,user_id) via Keycloak org claim
        (group-membership mapper; realm defines northwind-financial + acme-industrial groups)
        with the `users` mirror (by keycloak_sub, username/email fallback for seeded rows)
        updated on login; `get_default_org` deleted — every endpoint resolves via
        `repo.org_for(user)`; `Session.user_id` populated on /chat + /sessions; org predicates
        on sessions/chat/approvals/feedback/modules/overview/notifications/knowledge; two orgs
        + five users seeded (incl. bob.chen/eve.ops @ Acme Industrial). Flag
        `AEGISOPS_TENANCY=strict|legacy` (default strict). Evidence: `tests/test_tenancy.py`
        8/8 green (resolver matrix + endpoint isolation, cross-org 404); full backend suite
        402 passed / 2 skipped; vitest 25 passed. Note: existing dev Keycloak containers must
        be recreated to import the new groups/mapper; invalid-Gemini-key environments now seed
        with NULL embeddings + loud warning (keyword recall degrade, per invariant 7).
  - [x] **O2 · C1 · D1 · D4** (2026-07-12):
        **O2** — startup `assert_project` queries Langfuse `/api/public/projects` with the
        configured keys and warns loudly if they don't belong to `LANGFUSE_EXPECTED_PROJECT`
        (the "0 traces / wrong project" regression guard); best-effort, never blocks startup.
        **C1** — `test_module_ingress.py` asserts per cloud that the admin port binds to
        `allowed_cidr` only (never 0.0.0.0/0) and GCP firewalls attach via network tags (the
        regressed defect); source-level so it runs without creds. **D1** — migration
        `0005_hot_path_indexes` adds the four per-turn indexes; `test_indexes.py` verifies they
        exist and the transcript query plans onto one. **D4** — purged 11 tracked `aegisops.tfplan`
        files (they embed variable values) and gitignored `*.tfplan` + `terraform.tfstate.d/`;
        0 tracked plan/state remain. Durable dev-state-off-OneDrive is A3 (Phase 2). Evidence:
        `test_langfuse_tracing.py` (+3), `test_module_ingress.py` (6), `test_indexes.py` (2);
        full backend suite 438 passed / 2 skipped.
  - [x] **Honesty labels (P7/P8/P9)** (2026-07-12): no surface claims what it didn't do.
        **P8** — policy checks that the module enforces but the engine doesn't yet verify against
        the plan are marked `evaluated=False`/`passed=None` ("not evaluated"), never a green pass;
        real input predicates (encryption-off etc.) still show a real fail; the approval card and
        timeline count only genuinely-evaluated checks (`X/Y evaluated · N pending`). Real
        predicates over `terraform show -json` are Phase 2 (U1). **P7** — SRE remediation no longer
        returns `applied:True` after only listing; it reports `proposed_not_executed` and tells the
        user "proposed, not executed" (real K8s actions are Phase 2/U2). **P9** — the Traces tab
        returns no fabricated spans; it shows an honest note + a deep-link to the real Langfuse
        trace (trace_id==run_id); the real in-app tree is Phase 2 (O1). Evidence:
        `test_templates.py` (2), `test_honesty_labels.py` (2); full backend suite 427 passed /
        2 skipped; vitest 28.
  - [x] **U4 "Auto (ask me)" cloud default** (2026-07-12): the cloud selector now defaults to
        "Auto (ask me)", which maps to `cloud=null` on the wire (`cloudToWire`); `ChatContext.cloud`
        no longer defaults to AWS. So an ambiguous request (a generic VM with no cloud named) now
        reaches `resolve_cloud → None` and fires the clarifying question in the real UI, instead of
        silently provisioning on AWS (P11). `resolve_cloud` already handled null — only the default
        needed changing. Evidence: `cloud_selector.test.ts` (3) + existing
        `test_routing_scenarios.py::test_ambiguous_cloud_asks_never_defaults_to_aws`; full backend
        suite 424 passed / 2 skipped; vitest 28.
  - [x] **S5 execute-node capability assertion** (2026-07-12): the execute dispatcher fails closed
        unless the run is approved AND the recorded approver holds execute capability — a
        defense-in-depth check behind the approval gate. The approver's `can_execute` is carried
        through the `/approvals` resume value into the checkpointed state. (Asserts the approver,
        not the initiator: developers legitimately initiate without execute capability.) Evidence:
        `test_safety_invariants.py::TestExecuteCapabilityGuard` (3); full backend suite 424 passed / 2 skipped.
  - [x] **B5 terminal-state guarantee** (2026-07-12): both `_drive` closures now have an `except`
        that force-marks the run `failed` (via a self-guarded `_force_terminal`) if anything escapes
        — including a throw inside `_persist_result` — so a run is never left stuck in `running`
        (the B3 reconciler remains the outer backstop). `_force_terminal` only touches
        running/applying runs, never clobbering a terminal one. Evidence:
        `test_tenancy.py::TestTerminalStateB5` (3, incl. fault-injection through the real /chat endpoint).
  - [x] **B6 no blocking I/O on the event loop** (2026-07-12): `inventory.reconcile`'s sync boto3
        `describe_instances` (P6) now runs via `anyio.to_thread.run_sync`; the Gemini client no
        longer does a `models.list()` network call in its constructor (P18) — resolution is lazy
        and thread-offloaded on first generate/stream. Grep-audit confirms every other agent SDK
        call routes through the already-offloaded `tools/aws.py`. Evidence:
        `test_inventory.py::test_reconcile_offloads_blocking_sdk_call` (a concurrent ticker keeps
        ticking through a 0.4s blocking describe); full backend suite 418 passed / 2 skipped.
  - [x] **A4 org-scoped duplicate-name check** (2026-07-12): no logic change — `list_active` was
        always org-scoped, and S0 now flows the real authenticated org into `state["org_id"]`, so
        the same-name-create refusal is correctly org-bounded. Evidence:
        `test_inventory.py::test_duplicate_name_check_is_org_scoped` (org A's active name is
        invisible to org B; dup predicate fires only within the owning org).
  - [x] **A2 plan_guard at the approval choke-point** (2026-07-12): the approval node re-runs
        `check_plan_actions` before the durable interrupt, so a plan whose actions don't match the
        operation (an apply that would delete/replace, a destroy that would create, a read carrying
        a plan) is halted at the last gate — never shown to an approver, never applied — even if a
        plan path skipped the guard. Action derived from state (`execution_mode`/explicit `action`).
        Evidence: `test_safety_invariants.py::TestApprovalChokePointGuard` (4); full suite 416 passed / 2 skipped.
  - [x] **A1+B7 idempotency wait-or-abort** (2026-07-12): the in-flight-claim fall-through that
        could double-apply (P5) is closed — `cloudops_execute` now returns the stored result if the
        peer finished, WAITS up to a deadline if it's still applying, and ABORTS (never applies) if
        it still hasn't landed. New `idempotency.is_in_progress`/`wait_for_result` primitives (B7).
        `/approvals` gains an NX in-flight lock so a concurrent double-click is refused with 409
        before a second drive starts. Evidence: `test_idempotency.py` (+4, incl. a node test that
        asserts `runner.apply` is unreachable while a claim is in flight) +
        `test_tenancy.py::test_double_approval_endpoint_guard`; full backend suite 412 passed / 2 skipped.
  - [x] **S1 credential reveal hardening** (2026-07-12): reveal now requires initiator-or-approver
        + run org-scope (else 404, no enumeration) + a **mandatory step-up re-auth** (password
        re-entry → fresh Keycloak grant, ≤120s, `REVEAL_STEPUP_MAX_AGE_SECONDS`) + an **audit row
        on every attempt** (success and denial; value never logged). Frontend: a re-auth modal on
        the Reveal button that surfaces a 401 in place. Redis NX one-shot preserved. Evidence:
        `test_tenancy.py::TestCredentialRevealS1` (all six paths + audit-count == attempts); full
        backend suite 407 passed / 2 skipped; vitest 25; tsc clean.
  - [x] **A5 initiator + 4-eyes** (2026-07-11): migration `0004_run_initiated_by` adds
        `runs.initiated_by` (FK users.id) + `runs.env`; `/chat` records both; `/approvals`
        refuses Production self-approval with a clear 403 (flag
        `AEGISOPS_FOUR_EYES_FOR_PRODUCTION`, default on; legacy NULL-initiator runs skip).
        Evidence: `test_tenancy.py::test_four_eyes_blocks_prod_self_approval`; full backend
        suite 406 passed / 2 skipped.
  - [x] **S4 persist-time redaction backstop** (2026-07-11): `_persist_result` runs `redact()`
        on the answer and `redact_dict()` on the outcome before any DB write — a secret echoed
        by a future agent can no longer persist (P20). Evidence:
        `test_redaction.py::TestPersistBackstop`; full backend suite 405 passed / 2 skipped.
  - [x] **S3 /chat initiator gate** (2026-07-11): `POST /chat` now requires an initiator role
        (`require_initiator`) — read-only/auditor get a clear 403 and can still view everything;
        the composer shows an honest read-only notice instead of a dead input box
        (`can_initiate=false`). Evidence: `test_rbac_endpoints.py::test_chat_requires_initiator`;
        full backend suite 404 passed / 2 skipped; vitest 25 passed; tsc clean.
  - [x] **S2 read/stream authorization** (2026-07-11): shared `authorize_run`/`authorize_session`
        predicates (security/deps.py) applied on every run read — all 8 artifact tabs, the
        credentials endpoint, `/runs/{id}`, `/chat/stream/{id}` (authorized BEFORE attaching to
        the live channel), and `/sessions/{id}/messages`. Cross-org UUIDs → 404 (never 403, no
        enumeration); invalid UUIDs → 404, not 500. Evidence:
        `test_tenancy.py::test_cross_org_read_of_every_tab_is_404`; full backend suite
        403 passed / 2 skipped; vitest 25 passed.
- [ ] **Phase 2 — Production harness + Context Engine** (B1 B2 B3 B4 · A3 + latency pass ≤15s ·
      M1 M2 M3 M5 · U1 + defaults honesty · U2 U3 · O1 O3 · D2 U5 U8 · P16). Exit gate:
      worker-kill mid-apply recovers exactly once; multi-worker streaming; turn-20-of-100 recall
      in the UI; real failed policy check; real Traces tab; honest model menu.
- [ ] **Phase 3 — Intelligence layer** (D3 World Model + Reconciliation · dependency closure ·
      U6 Governed Executive Loop · read-only investigation agents · Module Promotion Pipeline ·
      M4 · U7 · modify-beyond-ports · cost estimation · P17). Exit gate: VPC→EC2 DAG demo e2e;
      drift notification; world-model destroy warning; module promotion flow.

### Phase 2 — EXIT GATE EVIDENCE (2026-07-12) — all checklist items done; awaiting owner sign-off

All Phase-2 items complete in checklist order (E2E-first → B1→B2→B3→B4→B5→A3→LAT→M1/M2/M3/M5→
U1→U2→U3→O1→O3→D2→U5→U8→P16), each with acceptance tests + full suite green + its own commit.
Gate demos ran on the **production posture: Redis event bus + reconciler on + two API workers**
(`docker-compose.override.yml`: `api` :8000 + `api-b` :8001, `AEGISOPS_EVENT_BUS=redis`,
`AEGISOPS_RECONCILER=on`).

1. **Multi-worker streaming (B1) — live.** Run `6ad9e056` driven + streamed on `api`; a
   reconnect to `GET /chat/stream/6ad9e056…` served by **`api-b` (a separate container)**
   returned 200 with the full replay from Redis Streams (run/step/token/error/done, stream ids
   `1783846428356-0`…), confirmed in api-b's access log. The `error` event is the honest
   invalid-Gemini-key surfacing (`llm_unavailable`, retriable) — the run still terminated
   cleanly. Contract parity: U8's full-vocabulary byte-identical-frames test.
2. **Worker-kill recovery (B2/B3) — live, and it caught a real defect.** First demo: SIGKILL
   `api` mid-run → worker B's reconciler logged `resumed` **and 60s later `marked_failed`** for
   the same run — `_redrive` never persisted its result (a redriven apply's outcome would have
   been overwritten). Fixed (redrive now persists via `_persist_result` + `done` + B5 backstop;
   commit 82e99d4) and re-demoed: run `828be32f` SIGKILLed mid-run → **exactly one** reconciler
   action (`reconciler.resumed` on api-b) → `completed` **with the persisted answer** (~91s:
   45s heartbeat TTL + sweep cadence). Mid-APPLY single-execution:
   `test_kill_mid_apply_recovers_once_without_reapply` (real PG/Redis: `applying` + claimed
   idempotency key → terminal once, claim untouched — never re-applied) + A1 wait-or-abort
   tests; a live chat-driven cloud apply is Gemini-key-gated at routing (see 8).
3. **Turn-20-of-100 recall IN THE UI — live.** 100 real turns seeded sequentially through the
   real `POST /chat` (session `6bec9355…`, every turn streamed to `done`); turn 20 = "Note for
   the record: the phoenix cluster runs 17 nodes in Mumbai…". In the UI: "What did I say in
   turn 20?" → **"Your 20th user in this conversation was:" + the verbatim sentence**
   (deterministic pre-LLM path — answered in seconds despite the dead LLM key). Playwright
   `e2e/gate-evidence.spec.ts` (assertion targets the answer's unique prefix, which cannot
   match seeded content); screenshot `frontend/e2e/gate-out/recall-turn-20.png`. Gate finding
   fixed en route: `_RECALL_RE` lacked the noun-first "turn N" shape (M2 amendment above).
4. **Real failed policy check (U1) — live at the plan seam.** Real `terraform plan` against the
   real Google provider (project `user-chlejclwsxdb`, `+2` resources, ~8s wall on warm init):
   `gcp.cloudsql` with `database_version=MYSQL_8_0` → **`Approved engine (PostgreSQL)`:
   `passed=false`, `detail=MYSQL_8_0`, `evaluated=true`** — and the unverifiable check honestly
   `evaluated=false` (pending), never a fake pass. Plan-JSON-driven failing predicates:
   `test_templates.py` (real captured `terraform show -json` fixture → `Public access blocked`
   `passed=false`). The chat hop above this seam (router/LLM extraction) is Gemini-key-gated.
5. **Real Traces tab (O1) — live in the UI.** Screenshot `gate-out/traces-tab.png`: the recall
   run's tree — `general run 6.7s` → `router 4.1s`, `general 2.6s`, `finalize 18ms`,
   `servicenow_update 6ms`, `notify 11ms` — real durations from `run_steps`, trace id, and the
   "Open in Langfuse" deep-link.
6. **Honest model menu (U3) — live.** Screenshot `gate-out/model-menu.png`: the menu lists
   exactly `gemini-3.5-flash (default ✓)`, `gemini-flash-latest`, `gemini-2.5-flash` — no
   phantom vendors. Live `GET /models` returns the same catalog; live `POST /chat` with
   `model=gpt-4o` → **400** "Unknown model 'gpt-4o'. AegisOps serves: …".
7. **Warm-turn latency (LAT) — measured.** Init cold **3.97s → warm 0.002s** (demo-null,
   recorded at the LAT item; ~19s saved per warm turn for real cloud providers per the prior
   audit). Live corroboration at the gate: the real GCP CloudSQL plan turn completed in **~8s**
   wall on a warm init. The full ≤15s cloud-provisioning turn end-to-end (LLM classify/extract +
   plan) still needs a valid Gemini key — the LLM hops currently ADD retry latency (honest
   degradation), so an end-to-end number today would measure the broken key, not the system.
8. **Suites.** Backend **527 passed / 2 skipped / exit 0** (final run, workers stopped during
   the suite); frontend vitest 28; Playwright: core-flow 5 + gate-evidence 4 (+ the 22-green
   browser suite recorded at Phase-2 entry).

**Environment-gated (recorded, never faked):** invalid `GEMINI_API_KEY` (chat-driven
provisioning routing + LLM extraction + embeddings degrade honestly and loudly — this is the
one blocker for the remaining live-UI variants of items 2/4); no AWS/Azure creds; no
`GITHUB_TOKEN`; no K8s cluster.

**Phase 2 signed off 2026-07-12** (gate accepted on the demos + 527 green).

---

## DEFERRED LIVE VERIFICATION (replay with fresh credentials — owner runs these with me)

Sandbox creds expire hourly, so every live-cred demo is deferred here with its exact replay
steps. Rules: each item names **Needs** (credentials), **Steps** (exact), **Expect** (pass
criteria). Phase-3 items append their own entries as they land. Nothing here is ever reported
as done without the live run; the code paths behind each are covered by tests with fakes.

- [ ] **DLV-1 · Chat-driven failed-policy approval card** — Needs: valid `GEMINI_API_KEY`
      (GCP SA already at `infra/secrets/gcp-sa.json`).
      Steps: (1) set the key in `.env`, `docker compose restart api api-b`; (2) login UI as
      `maya.okafor@northwind.com`; (3) send **"Create a Cloud SQL database in gcp,
      name=gate-fail-demo, database_version=MYSQL_8_0"**; (4) wait for the approval card;
      (5) **Reject** (never apply).
      Expect: routed → cloudops · real `terraform plan` (+2) · card shows policy check
      **"Approved engine (PostgreSQL)" FAILED (detail MYSQL_8_0)** with the other checks
      honestly evaluated/pending · reject halts the run.
- [ ] **DLV-2 · Live kill-mid-APPLY recovers once** — Needs: valid `GEMINI_API_KEY` (+ GCP SA
      present).
      Steps: (1) send **"Create a GCS bucket in gcp, bucket_name=aegisops-dlv2-<rand>"**;
      (2) approve on the card; (3) while the apply streams to the console,
      `docker compose kill api`; (4) watch from api-b (`GET /runs/{id}`): heartbeat expires
      ≤45s, next reconciler sweep recovers; (5) verify with a day-2 read ("what's the state of
      aegisops-dlv2-<rand>?"); (6) clean up via the gated destroy flow.
      Expect: **exactly one** reconciler action in api-b logs · run terminal ≤ ~105s with an
      HONEST outcome (applied-and-recorded, or aborted-no-double-apply via the A1 claim —
      never a fake success) · inventory row matches the real bucket state · destroy gated +
      applied.
- [ ] **DLV-3 · Full warm-turn ≤15s** — Needs: valid `GEMINI_API_KEY` (+ GCP SA).
      Steps: (1) run one GCS plan turn to warm init; (2) time message→approval-card for a
      second identical request (`time curl … POST /chat` to the `interrupt` event).
      Expect: **≤15s** (init≈0 measured 0.002s + cloud plan + LLM classify/extract).
- [ ] **DLV-4 · Remote Terraform state backend (S3)** — Needs: AWS creds + an S3 bucket
      (+ optional DynamoDB lock table).
      Steps: (1) set `AEGISOPS_TF_BACKEND=s3` + bucket/table env per `.env.example`; (2) run an
      apply; (3) `docker compose kill api` mid-run and let api-b recover it; (4) confirm state
      objects in S3, not the local volume.
      Expect: state in S3 · cross-worker recovery works against the remote backend ·
      `test_terraform_backend.py` semantics hold live.
- [ ] **DLV-5 · DevOps CI poll-to-completion (P16)** — Needs: `GITHUB_TOKEN` (+ `GITHUB_ORG`).
      Steps: (1) send "deploy repo=<org>/aegisops-demo branch=main env=dev"; (2) approve;
      (3) watch the console: `[ENSURE_CI_RUN] tracking run <id> → <url>`, then per-poll
      `CI run <id>: queued/in_progress`.
      Expect: polls the **dispatched run id** to completion · real conclusion drives the
      outcome (`success` → deployed; `failure` → pipeline failed honestly).
- [ ] **DLV-6 · SRE real K8s remediation (U2)** — Needs: kubeconfig to a live cluster +
      Prometheus scraping kube-state-metrics.
      Steps: (1) set `KUBECONFIG`; (2) send an incident ("orders-api is crashlooping");
      (3) approve the proposed `restart`; (4) `kubectl get deploy orders-api -o yaml | grep
      restartedAt`.
      Expect: real rollout-restart annotation patched · outcome `remediated/applied:true` with
      the real result · `recent_deploy` signal from the real generation-change query.
- [ ] **DLV-7 · Semantic (paraphrase) recall (M2)** — Needs: valid `GEMINI_API_KEY`.
      Steps: (1) in the seeded 100-turn session ask **"what did I note about the phoenix
      cluster's maintenance window?"** (no positional phrasing).
      Expect: embedding retrieval surfaces turn 20; the answer cites its content (pg_trgm
      keyword fallback already proven without the key).
- [ ] **DLV-8 · AWS/Azure template flows + plan-JSON policy checks live** — Needs: AWS and/or
      Azure creds.
      Steps: per cloud: plan→approve→apply→verify→gated destroy for one template (EC2 or S3;
      azure-storage), watching the card's policy checks (IMDSv2, encryption, TLS…) evaluate
      against the real plan JSON.
      Expect: checks real (pass AND fail when inputs are weakened) · apply/verify/destroy real.
- [ ] **DLV-9 · Cloud-level inventory orphan (D2 tail)** — Needs: a live TF backend (DLV-4).
      Steps: apply done but worker killed BEFORE the same-txn persist (kill during the final
      seconds of apply); after recovery, reconcile inventory from the Terraform state.
      Expect: the resource is discoverable and never silently invisible.
- [ ] **DLV-10 · Live drift / deleted-outside / orphan sweep (D3)** — Needs: AWS creds (EC2
      read) — or extend readers to the creds at hand; `AEGISOPS_DRIFT=on` on api/api-b.
      Steps: (1) provision an EC2 via the gated flow; (2) **manually change its security group
      in the AWS console** → within one sweep (≤60s) the bell shows "Drift detected: <name>"
      naming the changed field; (3) terminate the instance in the console → "Deleted outside
      AegisOps" (red); (4) launch an instance tagged `ManagedBy=aegisops` by hand →
      "Orphaned resource: i-…"; (5) confirm dedup: no repeat notifications on later sweeps.
      Expect: three finding kinds in the bell, org-scoped, deduplicated; world-model nodes
      annotated (`drift=true` + detail).
- [ ] **DLV-11 · World-model destroy warning in the UI (D3)** — Needs: valid `GEMINI_API_KEY`
      (+ any cloud creds, e.g. GCP).
      Steps: (1) provision a parent (VPC or resource group) and a dependent (EC2/storage)
      through the gated flow; (2) ask to **destroy the parent**; (3) inspect the approval card.
      Expect: policy check **"No dependent resources (world model)" FAILED** naming the
      dependent + the "⚠ Dependent resources" reasoning card + console warning; reject leaves
      everything untouched.
- [x] **DLV-13 · Real security scan in the promotion pipeline (MPP)** — **SATISFIED
      2026-07-12 by the SCAN commit** (no cloud creds were ever needed — only a scanner in
      the image, which the API image now bakes in: checkov 3.3.8 + tfsec v1.28.14).
      Automated permanently as `test_real_scan_gates_promotion_end_to_end`: a draft
      carrying an embedded credential (the canonical AWS docs EXAMPLE key) really FAILS
      checkov (CKV_SECRET_2 in the scan detail) → propose OK but promote REFUSED; a clean
      draft really PASSES. The fail-closed `unavailable` path is seam-forced in
      `test_promotion_is_blocked_without_a_passed_scan`.
- [ ] **DLV-15 · MODSEED gcp.vpc live lifecycle (MS-1)** — Needs: valid `GEMINI_API_KEY`
      (+ GCP SA present).
      Steps: (1) UI: "create a vpc named prod-net in gcp" (or `name=prod-net`); (2) approve on
      the card (checks: custom-mode PASS, ≥1 subnet PASS); (3) after apply: "what subnets does
      prod-net have" (day-2 read from recorded outputs incl. secondary range names);
      (4) "destroy prod-net" → gated destroy, impact_of consulted.
      Expect: full lifecycle through the governed pipeline; NAT + internal firewall visible in
      the GCP console; zero admin-ingress rules.
- [ ] **DLV-16 · MODSEED azure.vnet live lifecycle (MS-2)** — Needs: valid `GEMINI_API_KEY`
      + Azure creds.
      Steps: (1) "create a vnet named prod-vnet in azure" → approve (checks: ≥1 subnet, RFC1918,
      no-NSG all PASS); (2) day-2 "what subnets does prod-vnet have"; (3) "create a vm in
      prod-vnet" → placed into the existing VNet from the world model (provenance on the card);
      (4) gated destroy.
      Expect: NAT + route tables visible in the portal; zero NSGs from this module.
- [ ] **DLV-17 · MODSEED aws.nlb live lifecycle + create-first DAG (MS-3)** — Needs: valid
      `GEMINI_API_KEY` + AWS creds; `AEGISOPS_EXEC_LOOP=on` for the DAG case.
      Steps: (1) with one VPC in inventory: "create a load balancer web-lb in aws" → card
      shows DEP provenance (subnets from the VPC's recorded outputs) + the env default →
      approve → apply; (2) day-2: "what's the DNS name of web-lb" (recorded outputs incl.
      attach_targets_note); (3) with NO VPC: same request → goal-DAG card [aws.vpc → aws.nlb],
      ONE approval, both applied in order; (4) "destroy web-lb" → impact check + gated destroy;
      then destroying the VPC first must WARN (the NLB depends on it).
      Expect: cross-zone NLB + TCP TG in the console; egress-only SG; deletion_protection per
      env.
- [ ] **DLV-18 · MODSEED aws.kms live lifecycle (MS-4)** — Needs: valid `GEMINI_API_KEY`
      + AWS creds.
      Steps: (1) "create a kms key named app-secrets in aws" → approve (rotation/window checks
      PASS) → apply; (2) day-2: "what's the rotation on app-secrets" (from recorded outputs);
      (3) "destroy app-secrets" → the card states the scheduled-deletion window → approve →
      verify the key shows "Pending deletion" in the console (not gone).
      Expect: alias visible; key policy carries root + service statements only.
- [ ] **DLV-19 · MODSEED azure.keyvault live lifecycle (MS-5)** — Needs: valid
      `GEMINI_API_KEY` + Azure creds.
      Steps: (1) "create a key vault named app-vault in azure" → the card states the Allow
      network action + checks PASS → approve → apply; (2) day-2 "what's app-vault's uri";
      (3) gated destroy → card states soft-delete/purge semantics → verify the vault is
      soft-deleted (recoverable), not purged.
      Expect: AzureServices bypass + current-SP policy visible in the portal.
- [ ] **DLV-30 · P17 stakeholder emails (P17)** — Needs valid SMTP env
      (`SMTP_HOST/SMTP_USER/SMTP_PASSWORD`) + `GEMINI_API_KEY`.
      Steps: (1) initiator A submits an actionable run, approver B approves → the result
      email's To carries A AND B (not the from-address); (2) a run with no approval →
      only the initiator; (3) unset SMTP → the in-app bell notification still lands.
      Expect: From stays the configured sender; To never defaults to it while a
      stakeholder exists.
- [ ] **DLV-31 · PR-1 TF disk hygiene (PR-1)** — Needs AWS creds for a real apply.
      Steps: (1) run a create to terminal → its `aegisops-*-<run_id>.tfplan` is gone,
      runs.plan_json intact; (2) an awaiting_approval run KEEPS its plan; (3) destroy a
      resource, wait past the threshold (or call `sweep_tf_hygiene(max_age_days=0)`) →
      its empty state workspace dir is pruned, a day-2 on ANOTHER resource is unaffected;
      (4) a stray plan >7d is swept, a live run's plan is not.
      Expect: no prune ever fires inline at destroy or from a chat request.
- [ ] **DLV-32 · PR-2 limits (PR-2)** — Needs AWS creds (a genuinely slow apply) to see
      a real timeout; concurrency is verifiable without creds.
      Steps: (1) set `MAX_ACTIVE_RUNS_PER_USER=1`, start one run, submit a second →
      429 with the honest message, no second run row; kill the first worker → the count
      self-heals (the next submit succeeds without waiting for status cleanup);
      (2) set `TF_APPLY_TIMEOUT_S=5`, apply something real → fails within ~15s with the
      "exceeded … process group killed" classification, worker not hung, reconciler
      reconciles.
      Expect: awaiting_approval never consumes a slot.
- [ ] **DLV-29 · COST catalog estimate + guardrail on the live card (COST)** — Needs:
      valid `GEMINI_API_KEY` (any cloud creds optional — plan-only is enough).
      Steps: (1) `AEGISOPS_COST_GUARDRAIL_USD=50` in .env → restart api; (2) "create an
      ec2 named cost-probe, t3.medium" → the approval card shows "Cost estimate (catalog)
      $32.77/mo — static catalog estimate" and "Cost guardrail (≤ $50/mo)" PASS;
      (3) "create a postgres db named big-db, db.m5.large, 200 GiB" → the guardrail row
      FAILS ($147.83/mo vs the cap) and the approver still decides; (4) guardrail off →
      only the estimate row renders.
      Expect: numbers match agents/cost.py's catalog exactly; usage-based types (s3) show
      the honest zero-with-note. BACKLOG (owner 2026-07-13): Infracost integration
      replaces the static catalog later.
- [ ] **DLV-28 · MOD day-2 modify beyond ports + Option-A power state (MOD)** — Needs:
      valid `GEMINI_API_KEY` + AWS creds (+ GCP SA for the gce power pass).
      Steps: (1) "stop web-01" (an aws.ec2 from DLV-12/24) → the approval card shows a
      day-2 change "set power state to stopped (Terraform-managed, no SDK call)" with an
      IN-PLACE plan → approve → the instance shows stopped in the console;
      "start web-01" brings it back; (2) "stop az-box" (azure) → the honest answer
      ("use the portal…") with NO plan and NO SDK call — verify no run was created;
      (3) "turn versioning off on logs-bucket" → approve → suspended in the console;
      "expire objects after 30 days in logs-bucket" → the aegisops-expire rule appears;
      (4) "scale payments-db to db.t3.large" → in-place modify (plan_guard blocks any
      replace) → class changes without data loss; (5) "tag env=prod on web-01" → merged
      tags visible; (6) **live B1**: re-plan any pre-MOD resource from stored inputs →
      "No changes."
      Expect: every modify is approval-gated; power state changes appear in the plan diff
      as aws_ec2_instance_state / desired_status — never a bare API call.
- [ ] **DLV-27 · MODSEED azure.aks add-ons + azure.vm vnet placement live (MS-13)** —
      Needs: valid `GEMINI_API_KEY` + Azure creds.
      Steps: (1) "create an aks cluster named apps-aks with monitoring and calico" → the
      card states "Cluster monitoring: Log Analytics + OMS", "Network policy: calico on
      kubenet", "Azure Policy add-on" if chosen → approve → apply (~10 min) → verify in
      the portal: Insights receives logs (the -logs workspace), network policy calico,
      the policy add-on enabled; (2) with the DLV-16 azure.vnet in inventory: "create a
      vm named app-01 in my-vnet" → the card shows the vnet placement (slot provenance)
      → apply → the NIC sits in the existing vnet's first subnet and NO '<name>-vnet'
      was created; (3) **live moved-block B1**: a pre-enhancement VM re-planned from
      stored inputs → the plan shows the vnet/subnet address MOVES and "No changes.";
      (4) same for a pre-enhancement AKS cluster → "No changes."; (5) gated destroys.
      Expect: kube_config still works (RBAC pin unchanged); the vm's NSG stays attached
      via the NIC regardless of which vnet it landed in.
- [ ] **DLV-26 · MODSEED gcp.vm options + network placement live (MS-12)** — Needs:
      valid `GEMINI_API_KEY` + the working GCP SA.
      Steps: (1) with the DLV-15 gcp.vpc ("prod-network") in inventory: "create a vm named
      web-01 in prod-network" → the card shows the network placement (slot provenance, NOT
      the default-network row) → approve → apply → verify the instance + both firewalls sit
      in prod-network; (2) "create a shielded spot vm named batch-01" → the card states
      Shielded VM AND "may be STOPPED by GCP at any time … no automatic restart" → verify
      shielded config + SPOT provisioning in the console; (3) OS Login pass: enable_oslogin
      → SSH via IAM identity; the generated key is unused (stated on the card);
      (4) **live B1**: a pre-enhancement VM re-planned from stored inputs → "No changes.";
      (5) gated destroys.
      Expect: the one-time private-key reveal still works for standard VMs; spot VMs show
      provisioningModel=SPOT.
- [ ] **DLV-25 · MODSEED eks Auto Mode live cluster (MS-11)** — Needs: valid
      `GEMINI_API_KEY` + AWS creds + an existing VPC with private subnets (DLV-12's).
      Steps: (1) "create an eks cluster named apps-auto in auto mode, vpc <id>, subnets
      <ids>" → the approval card shows **"Cluster mode: EKS Auto Mode (API auth,
      general-purpose pool, auto-mode IAM policy set)"** → approve → apply (~10 min);
      (2) verify in the console: cluster Compute tab shows Auto Mode with the
      general-purpose node pool; access config = API; the cluster role carries the
      auto-mode policies (Compute/BlockStorage/LoadBalancing/Networking); NO managed
      node groups; (3) deploy a test pod → a node materialises without any node group;
      (4) **live B1**: a pre-enhancement standard cluster re-planned from stored inputs →
      "No changes."; (5) create a second cluster in standard mode → the old node-group
      path renders as before; (6) gated destroys.
      Expect: auto-mode clusters ignore instance_types/desired_size (the pool decides);
      the card's mode statement matches the console.
- [ ] **DLV-24 · MODSEED aws.ec2 SSM live session (MS-10)** — Needs: valid
      `GEMINI_API_KEY` + AWS creds.
      Steps: (1) "create an ubuntu vm named ops-box with session manager access" →
      the approval card shows **"Session Manager access available"** → approve → apply;
      (2) in the AWS console, Systems Manager → Session Manager → start a session to
      ops-box WITHOUT any SSH key/port (the SSM agent registers via the instance
      profile); (3) CloudWatch → confirm the agent's metrics namespace appears;
      (4) **live B1**: a pre-enhancement instance re-planned from stored inputs →
      "No changes." (no IAM resources appear); (5) gated destroy (verify the role/profile
      are removed with the instance).
      Expect: the SSH path still works independently (U1 CIDR-scoped) — SSM is additive.
- [ ] **DLV-23 · MODSEED gcp.cloudsql enhanced live lifecycle + CMEK slot (MS-9)** —
      Needs: valid `GEMINI_API_KEY` + the working GCP SA (Cloud SQL Admin API enabled).
      Steps: (1) with the gcp.kms ring from DLV-20 present: "create a cloudsql database
      named orders-db" → the DEP card OFFERS the ring's key as CMEK (provenance stated)
      → approve → apply; day-2 "what's the connection name for orders-db"; (2) with NO
      ring: the card states "Google-managed encryption (no CMEK)" — never forced;
      (3) secure-options pass: create one with backup_enabled + ssl_mode=ENCRYPTED_ONLY
      + the flag set → verify PITR/ssl/flags in the console; policy card shows
      "No world-open authorized networks" PASS for a scoped CIDR and **FAIL for the
      legacy 0.0.0.0/0 default** (the approver sees the honest fail); (4) private pass:
      private_network set → no public IP; (5) **live B1**: a pre-enhancement instance
      re-planned from stored inputs → "No changes." (the legacy `all` network keeps its
      name); (6) gated destroy (deletion_protection stays off so it completes).
      Expect: CMEK key must be same-region; the Cloud SQL service agent needs
      encrypter/decrypter on the key (grant via the ring's IAM var) — documented here so
      the live run grants it up front.
- [ ] **DLV-22 · MODSEED azure.db multi-engine live lifecycle + live moved-block B1 (MS-8)** —
      Needs: valid `GEMINI_API_KEY` + Azure creds.
      Steps: (1) "create a mysql database named orders-sql in azure" → routed to azure.db,
      card shows engine mysql / port 3306 → approve → apply → verify the flexible server +
      allow-azure firewall rule in the portal; (2) "create a sql server named ledger-mssql
      in azure" → mssql server + S0 database, TLS-1.2 check PASS; (3) day-2: "what's the
      admin username for orders-sql" (from recorded outputs; password only via the
      sensitive output path); (4) **live moved-block B1**: against a PRE-enhancement
      postgres server's real state, `terraform plan` from its stored inputs → the plan
      shows the address MOVE (`.this` → `.this["postgresql"]`) and **no changes**;
      (5) optional-capability pass: re-create one with `ha_enabled` + geo backup on and
      verify ZoneRedundant + geo-backup in the portal; (6) gated destroys (old-shape row
      destroy proves B3 live).
      Expect: engine families never mix; mssql requests with HA are rejected at the schema
      with the honest message.
- [ ] **DLV-21 · MODSEED aws.rds enhanced live lifecycle + live B1 re-plan (MS-7)** —
      Needs: valid `GEMINI_API_KEY` + AWS creds.
      Steps: (1) "create a mariadb database named orders-db, client cidr 10.20.0.0/16,
      with log exports" → the card shows port 3306, "DB security group scoped (no /0)"
      PASS, "Engine-aware log exports" stated → approve → apply; (2) day-2: "what's the
      connection string for orders-db" (sensitive output, no password — RDS-managed);
      (3) verify in the console: dedicated SG (only 10.20.0.0/16 on 3306), the
      `orders-db-logging-*` parameter group (slow_query_log), CloudWatch log groups;
      (4) **live B1**: `terraform plan` an instance created pre-enhancement from its
      stored inputs → "No changes." (the suite already proves the rendered-shape
      equivalence credential-lessly; this is the state-attached confirmation);
      (5) "create a postgres db named ledger, latest version" → the engine-version data
      source resolves the newest postgres; (6) gated destroys.
      Expect: multi-engine ports/exports correct per engine; the /0 rejection blocks a
      world-open request at BOTH schema and module validation.
- [ ] **DLV-20 · MODSEED gcp.kms live lifecycle (MS-6)** — Needs: valid `GEMINI_API_KEY`
      + the working GCP SA (`infra/secrets/gcp-sa.json`, Cloud KMS API enabled).
      Steps: (1) "create a kms keyring named app-ring in gcp" → approve (rotation/SOFTWARE/
      ENCRYPT_DECRYPT checks PASS) → apply; (2) day-2: "what's the rotation on app-ring"
      (answers 90 days from recorded outputs); (3) "destroy app-ring" → the card's Deletion
      semantics states rings are NOT deletable (versions/IAM only) → approve → verify in the
      console the ring still exists but its crypto-key versions are destroyed/disabled.
      Expect: `app-ring-key` created inside the ring with 90-day rotation; re-creating a ring
      with the same name in the same location fails (name permanently reserved) — the honest
      consequence the destroy card warned about.
- [ ] **DLV-12 · VPC→EC2 goal-DAG e2e in the UI (DEP+U6 — Phase-3 exit-gate headline)** —
      Needs: valid `GEMINI_API_KEY` + AWS creds; `AEGISOPS_EXEC_LOOP=on`.
      Steps: (1) send **"Create an EC2 named web in a new vpc"**; (2) inspect the goal-DAG
      approval card (step 1 `aws.vpc “web-net”` with its real plan summary; step 2 `aws.ec2
      “web”` marked "plans after parent", wired `subnet_id ← public_subnet_ids[0]`);
      (3) approve ONCE; (4) watch the per-step timeline: VPC plans+applies, then EC2 plans
      (real policy checks) + applies into the new VPC's real subnet; (5) verify with a day-2
      read; (6) gated destroy of the EC2 then the VPC (destroy order respects `impact_of` —
      destroying the VPC first must warn, DLV-11).
      Expect: one approval, both applied in order, wired to real outputs; partial-failure and
      deviation paths behave as the U6 tests specify if anything fails mid-run.

**STOPPED at the Phase-2 exit gate — awaiting owner sign-off before any Phase-3 work.**
_(Sign-off received 2026-07-12; Phase 3 started — see section S below.)_

Per-item status lives in the **`FIX.md §8` execution checklist** (the single progress tracker);
this section mirrors phase-level status only.

## S. Phase 3 — Intelligence layer (started 2026-07-12)

- [~] **Phase 3 in progress.** Checklist order: D3 world model → DEP → U6 executive loop →
      INV → MPP → M4 → U7 → MOD → COST → P17 → CLN-1 (final cleanup to the pure baked-image
      production posture). Same discipline: one item at a time → tests (fakes where live clouds
      are needed) → full suite green → FIX §8 + PROGRESS → commit. Live-cred demos go to the
      DEFERRED LIVE VERIFICATION list above with exact replay steps — never a faked live
      result. **STOP when all items are code-complete + suite green**, presenting the DLV list
      as one ordered end-to-end acceptance script.
  - [x] **D3 World Model + Reconciliation Engine** (2026-07-12): new `graph_db/world_model.py`
        — org-scoped `Resource` nodes (same merge key the context graph uses, enriched with
        org/TF-state refs) + `DEPENDS_ON` edges extracted by a PURE lookup over the resource's
        real inputs/outputs (`vpc_id`/`subnet_id(s)`/`security_group_ids`/`resource_group`/… —
        an edge can never be hallucinated; external parents become honest `status='external'`
        stubs). `impact_of(org, id|name)` answers "what depends on this?" — wired into the
        destroy card as a real policy check via `_world_model_impact_check`: FAILED + named
        dependents when they exist, passed only when consulted-and-clear, **pending (not a
        silent pass) when the graph is unreachable**, plus a "⚠ Dependent resources" reasoning
        card + console warning. Ingestion: `inventory.record_graph` upserts the world model on
        every apply; teardown marks it destroyed. New `agents/drift.py` reconciliation engine:
        per-(cloud,type) read-only reader seam (real cred-gated `Ec2Reader` ships; fakes in
        tests), curated-field `detect_drift` comparator, and a sweep producing org-scoped bell
        notifications for **drift** (amber) / **deleted-outside** (red) / **orphan** (P14 spend
        leak: `ManagedBy=aegisops` with no inventory row) — deduplicated 24h via Redis
        fingerprints, world-model nodes annotated best-effort (a down graph never drops
        findings or aborts the sweep). Runs inside the reconciler loop behind `AEGISOPS_DRIFT`
        (default off; tests drive `sweep()` explicitly, org-scoped to a throwaway org so the
        shared dev inventory is never polluted). Schema constraint ensured at startup.
        Evidence: `test_world_model.py` (9) + `test_drift.py` (8). Live sweeps + the UI destroy
        warning are **DLV-10/DLV-11**.
  - [x] **DEP dependency closure resolution** (2026-07-12): new `agents/dependency.py` — a
        declarative `Slot` map of the approved modules' REAL dependency fields (EKS
        `vpc_id`+`subnet_ids`, EC2 `subnet_id`, Azure `resource_group`) and `resolve_closure`
        applying the locked strict order: **named** (user's value untouched) → **world model**
        (one candidate → used, filled from its recorded outputs — an EC2 slot gets a real
        subnet id, never the VPC id; several → OFFERED with the real names/ids + "or say new";
        underivable facts → ask, never guess) → **stated default** (proceeds, stated on the
        card) → **create-first DAG** (parents-first ordered steps, child wired to the parent's
        real outputs — `subnet_id ← public_subnet_ids[0]`, RG by name; forced by "in a new
        vpc/rg"). Wired into `cloudops_plan` after validation: ask → clarification turn; dag →
        honest ordered proposal + `state["goal_dag"]` (the executive loop U6 executes it);
        complete → enriched inputs + "Dependency resolution" provenance rows on the approval
        card. Evidence: `test_dependency.py` (11) covering acceptance (a)–(d).
  - [x] **U6 Governed Executive Loop** (2026-07-12): new `agents/exec_loop.py`. PLAN phase
        (`plan_goal_dag`): validates the DAG (bounds `MAX_STEPS=5`; approved-catalog-only — no
        runtime-HCL escape hatch), terraform-plans + plan-guards + policy-checks every step
        whose inputs are concrete, honestly marks wired steps "planned at execute time", and
        raises **ONE approval interrupt** whose card lists every ordered step (frontend
        goal-DAG card added). EXECUTE phase (`execute_goal_dag` → `execute_governed_step` per
        step): deterministic core — resolve wires from PRIOR steps' real outputs
        (`public_subnet_ids[0]`, `input:name`), validate, plan, plan-guard (create-only),
        **real policy checks (a failing step halts, never auto-applies)**, apply, D2/D3
        bookkeeping, per-step timing (`loop_step_N_<key>`). **Idempotent across LangGraph
        interrupt-replays** via the A1 claim/stored-result store — a done step is never
        re-applied. A replanned step is a **deviation → fresh approval interrupt** showing
        was/now (reject → honest halt); `MAX_REPLANS_PER_STEP=1`. Partial failure reports
        "steps 1–N applied; step K failed: …" and never attempts later steps blind. Gated by
        `AEGISOPS_EXEC_LOOP` (default off → DEP's text proposal). Evidence:
        `test_exec_loop.py` (10). Live VPC→EC2 DAG e2e in the UI = **DLV-12** (exit-gate
        headline). tsc clean; vitest 28.
  - [x] **INV read-only investigation agents** (2026-07-12): new `agents/investigation.py` —
        the decision-13 boundary made structural. `ToolRegistry` ASSERTS read-only-ness at
        registration (13 mutation-marker shapes rejected outright) and freezes after build;
        `Investigator` can call only registered tools under a hard `MAX_CALLS=8` budget,
        returning every call as evidence (failed reads included — never crashes);
        **`spawn()` gives a sub-agent the SAME frozen registry and the SAME shared budget — a
        spawned agent is structurally unable to reach mutation or widen its surface**.
        `default_registry` holds the real reads (PromQL scalar, K8s list_deployments/pods,
        org inventory, world-model impact) — verified by IDENTITY to exclude the K8s mutating
        methods. SRE triage now gathers its K8s evidence through the investigator. deepagents
        (re-evaluate at 1.0/LTS) would plug in as a director over this registry. Evidence:
        `test_investigation.py` (21) + SRE remediation suite unchanged.
  - [x] **MPP Module Promotion Pipeline** (2026-07-12): new `agents/module_pipeline.py` +
        migration `0007_module_proposals`. Pipeline: `draft` stores the module files as INERT
        data (validated shape; catalog collisions and path escapes refused; **nothing planned
        or applied — generation and execution never share a turn**); `run_checks` runs REAL
        `terraform fmt -check` + `init -backend=false` + `validate` in an isolated scratch dir
        plus the security-scan seam (checkov preferred, tfsec fallback; neither installed here
        → honestly `unavailable`); `propose` requires green fmt+validate; `review` (approver
        RBAC at the API) either **promotes — which fails CLOSED without a PASSED scan** — or
        rejects (terminal). Promotion materializes the module under
        `infra/terraform-workspaces/promoted-<key>` and registers a runtime
        `WorkflowTemplate` (permissive `name`-required schema, honest `_todo` policy row; the
        plan-guard/approval/state-isolation machinery applies unchanged) — **only then** do
        `select`/`by_key`/`catalog` see it; `rehydrate_promoted` re-registers from the DB at
        startup. API: 5 org-scoped endpoints; frontend: proposals panel on the Infrastructure
        page (checks + status + Promote/Reject for approvers). Evidence:
        `test_module_pipeline.py` (9, real terraform runs). Live scan (checkov/tfsec install)
        = **DLV-13**.
  - [x] **M4 per-user/org persistent memory** (2026-07-12): new `agents/user_memory.py` +
        migration `0008_user_memory`. User-editable standing facts ("usual_region:
        ap-south-1") that survive sessions — org-scoped under S0, with org-wide rows (NULL
        user) visible to every member and the personal row winning on key collision.
        `build_context` now LEADS with the bounded (≤600 chars) standing block whenever
        org/user ids are supplied (router/general/knowledge pass them), so a brand-new
        session's very first LLM call already carries it; and the acceptance case is honored
        DETERMINISTICALLY too — `_extract_inputs` resolves "in my usual region" from the
        memory store with no LLM (explicit `region=` still wins; Azure templates map to
        `location`). API: `GET/PUT/DELETE /memory` (org-wide writes require an approver);
        frontend: "Standing memory" panel on Administration (list / Remember / forget).
        Evidence: `test_user_memory.py` (6) + all 14 memory regressions green.
  - [x] **U7 retry-with-fix + undo last apply** (2026-07-12): `provider_errors.suggest_retry`
        turns a classified failure into a one-click retry — the user's OWN message with only
        the fix applied (bad region → swapped in place, per-cloud alternates, never the failed
        region again; credentials-expired → same-message retry; anything without an honest fix
        → NO suggestion). The suggestion rides the `error` SSE event (`Emitter.error(retry=)`)
        from both cloudops failure paths; the frontend renders a "Retry with fix" button that
        re-sends it as a genuine new turn (full plan → policy → approval re-run). "Undo that"
        is a deterministic router fast-path (pre-LLM): cloudops destroy of `__last_applied__`,
        resolved by `inventory.last_applied` — SESSION-scoped, newest-first, destroyed rows
        skipped, honest refusal when this conversation applied nothing — then the normal gated
        destroy (approval card + D3 impact check + destroy-only plan guard); undo/revert now
        count as explicitly destructive. Defect caught en route: a literal backspace (0x08)
        had been written into the region-swap regex (invisible in code review) — found by the
        smoke test, fixed byte-level. Evidence: `test_retry_undo.py` (12). Live one-click
        retry + undo through the UI ride the existing DLV cloud items.
  - [x] **MODSEED MS-1 gcp-vpc (`gcp.vpc`)** (2026-07-12): first of the six MODSEED modules —
        custom-mode network + regional subnets (secondary pods/services ranges recorded in
        outputs for future GKE placement) + private_ip_google_access + optional flow logs +
        Cloud Router/NAT (ERRORS_ONLY logging) + an internal firewall scoped to the subnet
        CIDRs ONLY (no admin/SSH rules — the VM module owns admin ingress). Multi-file module
        (main/variables/outputs), no backend block, google `~> 5.40` (no bump), region from
        var. FULL registration in this one commit: template + `network` synonym,
        `GCPVPCInputs` (RFC1918-validated CIDRs), params (asks only `name`),
        `_gcp_vpc_policy` — REAL plan-JSON predicate (custom-mode on, ≥1 subnet; pending
        without a plan). Added the MODSEED **registry↔disk consistency test** (binds every
        module both directions). Evidence: `test_modseed_ms1_gcp_vpc.py` (9) incl. real
        fmt/validate on the dir + the seamless-contract integration (faked runner, live
        datastores): plan → real checks on the approval card → apply → inventory row +
        world-model node. Canary (B5): full suite + Playwright core-flow green. **Deliberate
        test update (recorded per B4 discipline):**
        `test_unsupported_combo_returns_none…[gcp-vpc]` pinned `("gcp","vpc")` as unsupported —
        MS-1 makes it supported BY DESIGN; the parametrize entry now uses `("gcp","lambda")`
        (permanently uncurated). Behavior change: a GCP network request no longer falls to the
        honest-catalog clarification — it provisions through the governed pipeline.
  - [x] **MODSEED MS-2 azure-vnet (`azure.vnet`)** (2026-07-12): VNet + public/private subnet
        tiers + NAT gateway (static Standard IP, private tier only) + public/private route
        tables associated BY NAME per tier (no explicit default route — Azure system routes
        already handle public egress, keeping the module free of any 0.0.0.0/0 literal). RG
        semantics identical to azure-vm (`<name>-rg` auto or existing). Deliberately NO NSG —
        the network module never carries an admin-ingress surface. FULL registration in one
        commit: template + `network`/`vnet` synonyms, `AzureVNetInputs` (RFC1918 validation on
        address space + both subnet tiers), params (asks only `name`), `_azure_vnet_policy`
        (REAL plan-JSON: ≥1 subnet, RFC1918 space, **zero NSG resources planned**), and the
        azure.vnet→resource_group DEP slot (same family as azure.vm). Evidence:
        `test_modseed_ms2_azure_vnet.py` (8) + MS-1 suite still green. Canary (B5): full suite
        + Playwright core-flow green. Live lifecycle = **DLV-16**.
  - [x] **MODSEED MS-3 aws-nlb (`aws.nlb`)** (2026-07-12): network LB (cross-zone on) + TCP
        target group (TCP health checks 30s/threshold 3/traffic-port) + TCP listener + an
        auto EGRESS-ONLY security group when none given (zero ingress rules — the single
        0.0.0.0/0 in the module is the outbound egress route). `deletion_protection` defaults
        ON for env=Production via the NEW `templates.apply_env_defaults` hook — resolved after
        validation, STATED on the card as an "Environment default" row, explicit choices win.
        Placement is DEP-resolved: one existing aws.vpc → `vpc_id` + `subnets` filled from its
        RECORDED `public_subnet_ids` (provenance on the card); two → offered; none →
        create-first DAG `[aws.vpc → aws.nlb]` wired to real outputs for the executive loop.
        `attach_targets_note` ships as a real output (an NLB without targets serves nothing —
        recorded with the resource, honest day-2 answer). Policy (plan-JSON): network type,
        cross-zone, TCP health shape, and **deletion-protection-as-approved** (a plan that
        quietly drops it FAILS). Evidence: `test_modseed_ms3_aws_nlb.py` (11) incl. the
        world-model DEPENDS_ON edge (impact_of names the NLB as the VPC's dependent). Canary
        (B5) green. Live lifecycle = **DLV-17**.
  - [x] **MODSEED MS-4 aws-kms (`aws.kms`)** (2026-07-12): KMS key (rotation ON, deletion
        window 7–30 bounded in BOTH the schema and the module) + `alias/<name>` + key policy
        (root admin via the real caller identity; allowed services — default
        secretsmanager/rds — get Decrypt/DescribeKey/CreateGrant). **Keys, never secrets**:
        secret VALUES are permanently out of scope (asserted — no password/secret_string
        anywhere). NEW `WorkflowTemplate.destroy_note` seam: the destroy approval card now
        carries the module's honest deletion semantics — for KMS, "enters its
        scheduled-deletion window, NOT removed immediately" (proven through the real
        destroy-plan path; MS-6's not-deletable key rings reuse the seam). Synonyms include
        **secrets→kms**. Policy (plan-JSON): rotation on + window ≥7, failing on rotation-off/
        short-window plans. Evidence: `test_modseed_ms4_aws_kms.py` (9) incl. the day-2
        rotation answer recorded in inventory attributes. Canary (B5) green. Live = **DLV-18**.
  - [x] **MODSEED MS-5 azure-keyvault (`azure.keyvault`)** (2026-07-12): vault (standard SKU,
        soft-delete 7–90 bounded in schema AND module, purge protection default ON,
        network_acls with AzureServices bypass) + current-SP access policy from the REAL
        client config + optional additional policies + optional RSA-2048 keys. **The vault,
        never secrets** — no secret_value / azurerm_key_vault_secret anywhere (asserted). RG
        via the same slot pattern as azure-vm/vnet. **network_default_action=Allow is STATED
        on the approval card** through the defaults hook ("accepts traffic from ALL
        networks"); Deny stays silent. destroy_note: soft-delete retention + "CANNOT be
        permanently purged until the window elapses". Policy (plan-JSON): soft-delete ≥7,
        purge-protection-as-approved, AzureServices bypass — all failing on violating plans.
        Evidence: `test_modseed_ms5_azure_keyvault.py` (10). Canary (B5) green. Live =
        **DLV-19**.
  - [x] **MODSEED MS-6 gcp-kms (`gcp.kms`)** (2026-07-12): key ring + crypto key(s) —
        for_each over `keys` (default one `<name>-key`), 90-day rotation derived
        `"${var.rotation_days * 86400}s"` (bounded 1–365 in schema, ≥1 in the module),
        ENCRYPT_DECRYPT + GOOGLE_SYMMETRIC_ENCRYPTION + SOFTWARE protection, IAM via
        setproduct(keys × encrypter_decrypters) → cryptoKeyEncrypterDecrypter. **Keys, never
        secrets** — no google_secret_manager / secret_data (asserted). Synonyms
        keyring/key/encryption_key/secrets→kms (gcp), coexisting with aws `key`→aws.kms.
        **destroy_note (MS-4 seam): GCP key rings are NOT deletable** — destroy removes
        crypto-key versions and IAM bindings only; the ring name stays reserved in the
        project permanently — asserted through the real destroy-plan card path. Policy
        (plan-JSON): rotation configured, SOFTWARE protection, ENCRYPT_DECRYPT purpose — all
        failing on an ASYMMETRIC_SIGN/HSM/no-rotation plan. Params ask only `name` (project
        auto-filled). Evidence: `test_modseed_ms6_gcp_kms.py` (9). Canary (B5) green. Live =
        **DLV-20**. **This completes MODSEED modules 1–6 — STOPPED for the evidence table.**
  - [x] **SCAN — checkov + tfsec across all terraform workspaces** (2026-07-12, owner
        pre-MS-7 condition): checkov 3.3.8 + tfsec v1.28.14 run over all 21 workspaces
        (20 cloud + demo-null). Raw findings: **156** (99 checkov / 57 tfsec). Every one
        fixed or waived — full ledger below. Enforcement: `infra/scan-workspaces.sh`
        (single source of truth), new **CI job `terraform-scan`**, and both scanners baked
        into the API image (checkov in an isolated venv at `/opt/checkov`, tfsec pinned
        binary) — **the MPP promotion gate is no longer environment-blocked**: `promote`
        now reaches a real scan verdict instead of fail-closed `unavailable` (DLV-13
        posture updated). The fail-closed path itself is now seam-forced in
        `test_promotion_is_blocked_without_a_passed_scan` (monkeypatched
        `_scan_command → None`) — a deliberate test change: the environment can no longer
        prove that path since the image always has a scanner (B4-style, recorded here).
        Both sweeps green with waivers applied: **checkov 0 / tfsec 0 across all 21**.
  - [x] **MODSEED MS-7 aws-rds enhanced (`aws.rds`)** (2026-07-12): multi-engine
        postgres/mysql/mariadb (validated at schema AND module), engine-aware port map
        (5432/3306/3306), engine-aware CloudWatch log exports + a query-logging parameter
        group (`for_each`, not `count` — checkov's graph follows for_each nodes, which is
        what lets CKV2_AWS_30 pass instead of being waived), `aws_rds_engine_version`
        data source count-gated to the "latest" pin or the logging family (credential-less
        plans never touch it), dedicated SG gated on MANDATORY `allowed_cidr` (`/0`
        rejected at schema and module; **`0.0.0.0/0` appears nowhere in the source**,
        test-pinned), optional subnet group, sensitive credential-free connection string
        (master password stays RDS-managed). **B1 gate is a REAL `terraform plan`**:
        old-shape stored inputs → schema-validated (every new field explicit at its B2
        old-behavior default) → exactly `{aws_db_instance.this: [create]}`, all new
        capability rendered null, every old attribute equal — via a test-only
        `*_override.tf` (fake creds + skip flags) removed in finally. **B2 pattern
        locked for MS-8..13: the SCHEMA defaults old behavior, the MODULE defaults
        secure** (checkov evaluates module defaults — that is exactly what lets waivers
        die without breaking B1). **Owner-binding first proof: the rds log-export
        waivers (CKV_AWS_129, CKV2_AWS_30) are REMOVED** — aws-rds checkov: 17 passed /
        0 failed with six non-MS skips; tfsec clean. **Stale-waiver guard shipped**:
        `test_scanner_waiver_guard.py` (FIX.md §8 done-rows vs every scanner config;
        api-test now ro-mounts `../FIX.md`) + an MS-1..13-range typo check; all
        shipped-design citations (MS-1/3/5/6) reworded to stand alone. Policy additions:
        "DB security group scoped (no /0)" (plan-aware + input fallback), "Engine-aware
        log exports". Params now offer engine_version/allowed_cidr/enable_log_exports
        (required set unchanged: identifier only). Evidence:
        `test_modseed_ms7_aws_rds.py` (8) + `test_scanner_waiver_guard.py` (2).
        Canary (B5) green. Live = **DLV-21**.
  - [x] **MODSEED MS-8 azure-postgres → azure.db (`azure.db`)** (2026-07-12): multi-engine
        postgresql/mysql/mssql, one family per instance, each `for_each`-gated —
        postgresql IS the pre-enhancement family with **`moved` blocks** migrating the old
        unkeyed addresses (`.this` → `.this["postgresql"]`) so real state re-plans as a
        no-op rename; mysql flexible (storage_mb→size_gb, default 8.0.21); mssql logical
        server + S0 database (TLS 1.2, lifecycle preconditions honestly rejecting HA /
        delegated-subnet for mssql). Optional HA (ZoneRedundant), geo-redundant backup
        (module default ON — kills the waiver; schema default OFF per B2), delegated
        subnet + private DNS (flips public access off and removes the allow-azure
        firewall rule). Generated `random_password` KEPT, shared by every engine.
        **B1/B2 gate = committed native `terraform test`** (mock providers, offline;
        azurerm acquires AAD tokens eagerly at configure so the MS-7-style fake-creds
        plan is impossible): `tests/b1_backcompat.tftest.hcl`, 6 runs green. **B3 proven
        end-to-end**: workspace dir UNCHANGED; `by_key("azure.postgres")` aliases to
        azure.db; an old-shape inventory row destroy resolves via synonym, the runner
        takes the ROW's workspace, old inputs validate at B2 defaults.
        `AzurePostgresInputs` aliases `AzureDBInputs`. Routing/params test rows updated
        deliberately (B4): azure postgres/database/db/sql/mysql/mssql/sqlserver →
        azure.db. Waivers: CKV_AZURE_136 REMOVED; CKV2_AZURE_26/57 re-justified without
        MS tags; 10 net-new mssql/mysql findings triaged (ledger). Scans green.
        Evidence: `test_modseed_ms8_azure_db.py` (9). Canary (B5) green. Live = **DLV-22**.

  - [x] **MODSEED MS-9 gcp-cloudsql enhanced (`gcp.cloudsql`)** (2026-07-12): every
        option variable-driven — authorized_networks (schema preserves the legacy
        world-open `all` entry verbatim, value AND name, so existing instances re-plan
        with zero renames; the MODULE default is none), private_network (drops the
        public IP + networks), ssl_mode (module default ENCRYPTED_ONLY; provider 5.x
        replaced require_ssl), backup_enabled (+PITR; module default ON),
        database_flags (module default = the full 10-flag pg observability set incl.
        pgAudit), enable_query_insights, maintenance window, deletion_protection var,
        **CMEK via a DEP slot on gcp.kms** (required=False, `attr:key_ids[0]`,
        stated default "Google-managed encryption (no CMEK)" — offered when a ring
        exists, never forced). Generated root password KEPT. **B1/B2 gate = committed
        native `terraform test`** (mock providers, 5 runs green). Policy: "No world-open
        authorized networks" **fails visibly on the legacy default**; private/backup/
        CMEK stated. **Waivers REMOVED**: CKV_GCP_14 + CKV_GCP_11 (checkov), and on the
        tfsec side the entire pg-flag family + backups + the world-open-network finding
        (tfsec evaluates the module's secure defaults through the dynamic block).
        Checkov's flag family re-justified as a documented scanner limitation (cannot
        expand dynamics inside `settings`; the terraform test asserts all 10 flags
        render). Evidence: `test_modseed_ms9_gcp_cloudsql.py` (8). Canary (B5) green.
        Live = **DLV-23**.

  - [x] **MODSEED MS-10 aws-ec2 SSM (`aws.ec2`)** (2026-07-12): optional SSM Session
        Manager + CloudWatch agent instance profile — IAM role with the two AWS managed
        policies + instance profile, the whole chain `for_each`-gated on `enable_ssm`
        (schema default FALSE per B2, named verbatim in the spec; module default TRUE —
        which is what kills the scanner waiver). The card states "Session Manager access
        available" when on (`_ec2_policy`). **B1 gate = committed native `terraform
        test`** (mock provider + `override_data` for the default-subnet discovery;
        isolated `TF_DATA_DIR` keeps tests off the workspace's A3 backend pointer).
        **Waiver REMOVED: CKV2_AWS_41** (aws-ec2 checkov 24/0; tfsec clean).
        Evidence: `test_modseed_ms10_aws_ec2_ssm.py` (6). Canary (B5) green.
        Live = **DLV-24**.

  - [x] **MODSEED MS-11 eks-provision Auto Mode (`aws.eks`)** (2026-07-13): `eks_mode =
        standard | auto` — mode wiring lives in root locals passed to the registry
        module: standard renders the EXACT pre-enhancement inputs (nulls leave the
        module's own defaults untouched; the `app` node group verbatim — B1), auto
        forces API authentication + `cluster_compute_config` with the general-purpose
        pool + no bootstrap self-managed addons + zero node groups (the registry module
        wires elastic-LB/block-storage and the auto-mode IAM policy set; `~> 20.8`
        already floats to the 20.31+ releases carrying Auto Mode — no pin change).
        **B1 gate = committed native `terraform test` with `override_module`** (the
        registry module output-mocked; OUR wiring asserted through locals). Schema
        default standard (B2 verbatim); the card states the mode ("Cluster mode" in
        `_eks_policy`); params offer the choice. No waiver changes (CKV_TF_1 was never
        MS-tagged). Scans green. Evidence: `test_modseed_ms11_eks_auto_mode.py` (5).
        Canary (B5) green. Live = **DLV-25**.

  - [x] **MODSEED MS-12 gcp-gce options + B4 network slot (`gcp.vm`)** (2026-07-13):
        shielded VM / OS Login / spot / least-scope SA / public-IP toggle, all
        variable-driven — schema B2 old-defaults (public IP ON, network "default",
        options off) while the MODULE defaults are secure (shielded ON, project-wide
        keys BLOCKED, no public IP). Spot's card check states the maintenance
        implications verbatim; OS Login's states the generated key becomes unused (the
        key + one-time reveal are KEPT). The `network` var drives the instance and BOTH
        firewalls. **B4 (recorded): new gcp.vm→network DEP slot** — "create a vm in
        prod-network" places into the EXISTING network; the DEF default-network row
        became conditional (only the default placement is flagged) — the one deliberate
        test change, no test weakened. **B1 = committed native `terraform test`**
        (5 runs). **ALL gcp-gce scanner waivers DELETED** (both config files removed;
        bare scans 19/0 + clean). Evidence: `test_modseed_ms12_gcp_gce.py` (8).
        Canary (B5) green. Live = **DLV-26**.

  - [x] **MODSEED MS-13 azure-aks add-ons + B4 azure.vm→vnet slot** (2026-07-13, the
        LAST MODSEED item): Log Analytics workspace + OMS agent / calico-on-kubenet /
        `azure_policy_enabled`, all variable-driven — schema B2 defaults OFF, module
        defaults ON (killing CKV_AZURE_4/7/116 + AVD-AZU-0040/0043; 12 sandbox-posture
        skips remain, none MS-tagged). **B4: azure.vm→vnet DEP slot** — "create a vm in
        my-vnet" lands the NIC in the existing vnet's first recorded subnet and the
        module skips its dedicated vnet+subnet (count + `moved` blocks migrate existing
        state); coexists with the RG slot; no existing test needed changing. **B1 gates =
        committed native `terraform test` in BOTH workspaces** (aks 4 runs, vm 2 runs).
        Discovery recorded: checkov auto-discovers a directory's own `.checkov.yaml` —
        "bare" in-place scans silently load the waivers, so waiver-death proofs use a
        config-free copy. Evidence: `test_modseed_ms13_azure_aks.py` (9). Canary (B5)
        green. Live = **DLV-27**. **MODSEED MS-7..13 COMPLETE — STOPPED for the evidence
        table + the VM start/stop options.**

  - [x] **MOD — day-2 modify beyond ports + Option-A power state** (2026-07-13):
        `_extract_modification` + `_MODIFY_CAPS` + `_apply_modification` generalize
        `_modify_resource` — s3 versioning/lifecycle/tags, rds scaling/tags, ec2
        ports/power/tags, gcp ports/power, azure ports — same gates as ever (approval,
        plan_guard in-place, policy re-run, per-resource state). **Power = Terraform-
        encoded per the owner's Option A** (`aws_ec2_instance_state` / GCE
        `desired_status`; `""` = unmanaged, B1); **Azure power answers honestly** (portal;
        no SDK fallback, no plan). Router: start/stop is ALWAYS modify. Committed tftest
        gates extended deliberately (ec2 4, gce 6, s3 NEW 3 — lifecycle expiry never a
        module default); CKV_AWS_300 fixed at the source. Tool discovery: `desired_status`
        expressions break checkov/tfsec default-rendering → 3+2 gcp-gce waivers re-added
        with TOOL-LIMITATION reasons (secure defaults unchanged, tftest-asserted).
        Evidence: `test_mod_day2.py` (11). Canary (B5) green. Live = **DLV-28**.

  - [x] **COST — static catalog estimation + guardrail** (2026-07-13, owner
        speed-directive scope): `agents/cost.py` static provider-pricing catalog (labelled
        "static catalog estimate" on every row — never a quote), honest unpriced/usage-based
        states, power/spot-aware; `AEGISOPS_COST_GUARDRAIL_USD` adds a REAL policy check
        failing on breach and failing closed when unpriced; wired into both plan paths.
        **Infracost integration → BACKLOG (owner directive).** Evidence:
        `test_cost_estimation.py` (7). Live = **DLV-29**.

  - [x] **P17 — notify real recipients** (2026-07-13): fixed the sender-as-recipient
        defect; `notify._recipients` addresses initiator + approver (approver email carried
        on the approval resume payload), from-address is the logged last-resort only.
        Evidence: `test_notify_recipients.py` (6). Live = DLV-30.
  - [x] **PR-1 — TFHYGIENE** (2026-07-13): terminal runs delete their .tfplan (record lives
        in plan_json; awaiting_approval keeps its plan); reconciler sweeps strays >7d
        (skipping non-terminal runs) and prunes destroyed resources' empty state workspaces
        (sweeper-only, reads state file directly, refuses non-empty, clears the pointer).
        Evidence: `test_pr1_tf_hygiene.py` (7). Live = DLV-31.
  - [x] **PR-2 — LIMITS** (2026-07-13): heartbeat-derived active-run counts (self-healing,
        no drift), 429 at the org/user cap before any write; per-stage subprocess timeouts
        with SIGTERM→grace→SIGKILL on the process group and an honest rc-124 classification.
        Evidence: `test_pr2_limits.py` (7). Live = DLV-32.

### Scanner ledger (fix or waiver per finding — owner condition, 2026-07-12)

Scanners: checkov 3.3.8, tfsec v1.28.14. Waivers live per-workspace in
`.checkov.yaml` (`skip-check`) and `.tfsec/config.yml` (`exclude`, legacy IDs — v1.28
config matching; the AVD id sits in each entry's comment). Every waiver is commented at
the site with the same reason as this ledger. New findings fail CI until fixed or triaged.

**Zero-finding workspaces (7):** aws-kms · azure-vnet · azure-resource-group · azure-vm†
· demo-null · gcp-kms† · gcp-vpc† — († = checkov-clean; tfsec/checkov had the entries
listed below.) aws-kms and azure-vnet were fully clean in BOTH scanners.

**FIXED (6 — all verified zero-plan-impact except the two noted):**

| Workspace | Finding(s) | Fix |
|---|---|---|
| aws-ec2 | CKV_AWS_23 / AVD-AWS-0124 (SG rule descriptions) | descriptions added to the open-ports ingress + egress rules. In-place rule update (rule recreation on AWS for inline rules — no live resources exist yet). |
| aws-nlb | AVD-AWS-0099 + CKV_AWS_23 / AVD-AWS-0124 (SG + rule descriptions) | description on the egress-only SG + its egress rule. NOTE: SG `description` forces replacement on AWS — changed before any live NLB exists. |
| aws-rds | CKV_AWS_226 (auto minor upgrades) | `auto_minor_version_upgrade = true` — explicit form of the provider/API default, zero plan impact. |
| azure-aks | AVD-AZU-0042 CRITICAL (RBAC) | `role_based_access_control_enabled = true` — explicit form of the azurerm default, zero plan impact. |
| gcp-gke | CKV_GCP_9/10 + AVD-GCP-0063/0058 (node auto-repair/upgrade), CKV_GCP_13 (client cert auth) | explicit `management { auto_repair/auto_upgrade = true }` + `master_auth { issue_client_certificate = false }` — both explicit forms of GKE defaults, zero plan impact. |
| gcp-gcs | CKV_GCP_114 (public access prevention) | `public_access_prevention = "enforced"` — real in-place hardening, aligned with the platform's existing no-public-bucket policy (U1). |

**EXCLUDED AS UPSTREAM (not our source):** aws-vpc AVD-AWS-0102×4 / 0105×2 / 0178 and
eks-provision AVD-AWS-0038×2 / 0104 sit inside the version-pinned registry modules'
downloaded caches (`.terraform/modules/...`) — excluded via `--skip-path '\.terraform'`
(checkov) and `--exclude-downloaded-modules` (tfsec). Upstream modules are scanned by
their own projects; pins are exact versions (CKV_TF_1 on both registry sources is waived
for the same reason — commit-hash pinning applies to git sources).

**WAIVED (per-workspace; scanner IDs grouped where both flag the same concern):**

| Workspace | Finding(s) | Waiver reason |
|---|---|---|
| aws-ec2 | ~~CKV2_AWS_41~~ | **WAIVER REMOVED by MS-10 (2026-07-12)** — the SSM+CloudWatch instance-profile chain ships for_each-gated (module default ON; schema keeps existing instances unchanged per B2); checkov follows the graph and passes. |
| aws-ec2 | CKV_AWS_126 | Detailed monitoring: sandbox cost posture; opt-in candidate. |
| aws-ec2 | CKV_AWS_135 | EBS-optimized is instance-type dependent; modern types are optimized by default. |
| aws-ec2 | CKV_AWS_382 / AVD-AWS-0104 | Egress-open by design (outbound updates); ingress is the guarded surface — U1 enforces the SSH-CIDR policy. |
| aws-nlb | CKV2_AWS_20 | HTTP→HTTPS redirect is an ALB-listener concept; this is a network (TCP) LB. |
| aws-nlb | CKV_AWS_150 | Deletion protection is variable-driven; Production env-default ON + the deletion-protection-as-approved policy check (MS-3 design). |
| aws-nlb | CKV_AWS_91 | Access logging needs an S3 bucket dependency; opt-in candidate, never forced. |
| aws-nlb | CKV_AWS_382 / AVD-AWS-0104 | Egress-only SG by design; zero ingress rules (MS-3 source invariant). |
| aws-nlb | AVD-AWS-0053 | Internet-facing is the module's purpose; `internal = true` is variable-driven. |
| aws-rds | ~~CKV2_AWS_30 + CKV_AWS_129~~ | **WAIVER REMOVED by MS-7 (2026-07-12, owner-binding first proof)** — the module ships engine-aware log exports + a query-logging parameter group, secure-by-default at module level; checkov passes both checks with zero skips. |
| aws-rds | CKV2_AWS_60 | Copy-tags-to-snapshot: day-2 candidate; enabling would alter existing instances' re-plans (B1). *(Re-justified without the MS tag when MS-7 shipped.)* |
| aws-rds | CKV_AWS_118 + CKV_AWS_353 / AVD-AWS-0133 | Enhanced monitoring / performance insights: sandbox cost posture. |
| aws-rds | CKV_AWS_157 | Multi-AZ: sandbox cost posture; opt-in candidate. |
| aws-rds | CKV_AWS_161 / AVD-AWS-0176 | IAM auth: opt-in candidate; master credentials are AWS-managed (`manage_master_user_password`). |
| aws-rds | CKV_AWS_293 / AVD-AWS-0177 | TF-level deletion protection would fail governed destroy runs mid-apply; destroys are approval-gated by the platform instead (same reasoning as gcp-kms CKV_GCP_82 and gke's explicit `deletion_protection = false`). |
| aws-rds | AVD-AWS-0077 | Backup retention: day-2 candidate; changing it would alter existing instances' re-plans (B1); sandbox cost posture. *(Re-justified without the MS tag when MS-7 shipped.)* |
| aws-s3 | CKV2_AWS_61 | Lifecycle rules: the OPTION now exists (`lifecycle_expire_days`, MOD) but never as a module default — auto-expiring objects is a data-loss decision the user makes explicitly; the check judges the off-default rendering. |
| aws-s3 | CKV2_AWS_62 | Event notifications: no consumer exists in the platform's flows. |
| aws-s3 | CKV_AWS_144 | Cross-region replication: not in any binding scope (explicitly out of MODSEED). |
| aws-s3 | CKV_AWS_145 / AVD-AWS-0132 | SSE-S3 (AES256) is the module contract; KMS default would add key cost/coupling. |
| aws-s3 | CKV_AWS_18 / AVD-AWS-0089 | Access logging needs a second bucket; demo posture, opt-in candidate. |
| aws-vpc | CKV_TF_1 | Registry module `terraform-aws-modules/vpc` pinned by exact version (see upstream note). |
| eks-provision | CKV_TF_1 | Registry module `terraform-aws-modules/eks` pinned by exact version (see upstream note). |
| azure-aks | ~~CKV_AZURE_4 / AVD-AZU-0040 + CKV_AZURE_7 / AVD-AZU-0043 + CKV_AZURE_116~~ | **WAIVERS REMOVED by MS-13 (2026-07-13)** — module defaults: Log Analytics + OMS on, calico on kubenet, Azure Policy add-on on (schema keeps existing clusters unchanged per B2). Proven on TRUE-bare scans (a config-free copy — checkov auto-discovers a directory's own `.checkov.yaml`, so in-place scans are never bare). |
| azure-aks | CKV_AZURE_115 + CKV_AZURE_6 / AVD-AZU-0041 | Private cluster / authorized IP ranges would cut the demo's public API access path. |
| azure-aks | CKV2_AZURE_29 | kubenet is the sandbox default; Azure CNI is a plan-shape change (network profile). |
| azure-aks | CKV_AZURE_117 | Disk encryption set: platform-managed keys are the sandbox posture. |
| azure-aks | CKV_AZURE_141 | Local admin stays enabled: `kube_config` output is the demo access path. |
| azure-aks | CKV_AZURE_168 | Max-pods sizing: sandbox node defaults. |
| azure-aks | CKV_AZURE_170 | Paid SLA SKU: sandbox cost posture. |
| azure-aks | CKV_AZURE_171 | Upgrade channel: pinned-version posture; owner-controlled upgrades. |
| azure-aks | CKV_AZURE_172 | CSI secret autorotation: CSI driver not enabled in this module. |
| azure-aks | CKV_AZURE_226 | Ephemeral OS disks are VM-size dependent; sandbox sizes may not support them. |
| azure-aks | CKV_AZURE_227 | Host encryption requires subscription feature registration; sandbox posture. |
| azure-aks | CKV_AZURE_232 | Single-pool sandbox cluster; system/user pool split is an enterprise topology. |
| azure-keyvault | CKV_AZURE_109 / AVD-AZU-0013 + CKV_AZURE_189 + CKV2_AZURE_32 | Network posture is variable-driven and honestly STATED on the approval card when Allow (MS-5 design); AzureServices bypass + ACLs are the guard; private endpoint is an enterprise opt-in. |
| azure-keyvault | CKV_AZURE_112 | Standard SKU (software-protected keys) is the module contract; premium/HSM is a cost decision. |
| azure-keyvault | CKV_AZURE_40 | Expiration on the optional RSA keys: opt-in candidate; rotation semantics owner-decided. |
| azure-postgres | ~~CKV_AZURE_136~~ | **WAIVER REMOVED by MS-8 (2026-07-12)** — the module defaults geo-redundant backup ON (the platform schema keeps old behavior for existing resources per B2); checkov passes with zero skips. |
| azure-postgres | CKV2_AZURE_26 | The 0.0.0.0–0.0.0.0 firewall rule is Azure's allow-azure-services sentinel, not world-open; the private path (`delegated_subnet_id`) removes the rule entirely. *(Re-justified without the MS tag when MS-8 shipped the private-access option.)* |
| azure-postgres | CKV2_AZURE_57 + CKV2_AZURE_56 | Private access ships via `delegated_subnet_id` + `private_dns_zone_id` (postgresql/mysql); the default stays public for existing resources' re-plans (B1/B2) — a module cannot fabricate network identity. *(Re-justified when MS-8 shipped.)* |
| azure-postgres | CKV_AZURE_113 + CKV2_AZURE_45 | mssql public network / private endpoint: the demo access path; SQL Server private connectivity is private-endpoint class, documented out of module scope (a lifecycle precondition says so). *(New with MS-8's mssql family.)* |
| azure-postgres | CKV2_AZURE_34 | mssql firewall 0.0.0.0–0.0.0.0 is the allow-azure-services sentinel, not world-open. *(New with MS-8.)* |
| azure-postgres | CKV2_AZURE_27 | mssql AAD auth needs a caller-supplied AAD admin identity; opt-in candidate. *(New with MS-8.)* |
| azure-postgres | CKV_AZURE_23 + CKV_AZURE_24 + CKV2_AZURE_2 | mssql auditing / retention / vulnerability assessment all need a log storage-account (+ Defender) dependency; opt-in candidates, never forced. *(New with MS-8.)* |
| azure-postgres | CKV_AZURE_224 | SQL Ledger: niche compliance feature; not part of the module contract. *(New with MS-8.)* |
| azure-postgres | CKV_AZURE_229 | Zone-redundant database requires premium tiers; S0 is the sandbox sku. *(New with MS-8.)* |
| azure-storage | CKV2_AZURE_1 | CMEK: platform-managed keys are the sandbox posture. |
| azure-storage | CKV2_AZURE_33 | Private endpoint: sandbox posture. |
| azure-storage | CKV2_AZURE_38 | Blob soft-delete: day-2 candidate; enabling now would alter existing accounts' re-plans. |
| azure-storage | CKV2_AZURE_40 | Shared-key auth: SDK verify paths use account keys; revisit with managed identity. |
| azure-storage | CKV2_AZURE_41 | SAS expiration policy: no SAS tokens are issued by the platform. |
| azure-storage | CKV_AZURE_206 | LRS replication: sandbox cost posture. |
| azure-storage | CKV_AZURE_33 | Queue-service logging: queues unused by the platform. |
| azure-storage | CKV_AZURE_59 | Public network access: container ACLs are private; account-level lockdown is an enterprise opt-in. |
| azure-vm | CKV_AZURE_119 | Public IP by design: the demo access path is SSH to the VM (U1 CIDR-scoped admin ingress). |
| azure-vm | CKV_AZURE_151 | Module provisions Linux VMs; the Windows-encryption rule does not apply. |
| azure-vm | CKV_AZURE_50 ×2 | Extension operations stay allowed: provisioning/verify flows may install agents. |
| gcp-cloudsql | ~~CKV_GCP_14 / AVD-GCP-0024~~ | **WAIVER REMOVED by MS-9 (2026-07-12)** — the module defaults backups+PITR ON (schema keeps old behavior per B2); both scanners pass. |
| gcp-cloudsql | ~~CKV_GCP_11 + tfsec pg-flag family (AVD-GCP-0014/0016/0020/0022/0025) + one AVD-GCP-0017~~ | **WAIVERS REMOVED by MS-9** — module defaults: NO authorized networks (the world-open finding dies) and the full 10-flag observability `database_flags` set, which tfsec evaluates through the dynamic block and passes. |
| gcp-cloudsql | CKV2_GCP_13, CKV_GCP_51–54, CKV_GCP_108–111 | **Scanner limitation, not a missing capability**: the flags ship in the module-default `database_flags` map, but checkov cannot expand dynamic blocks inside `settings` (tfsec sees them and passes). The committed terraform test asserts all 10 flags render — regression-proof. *(Re-justified when MS-9 shipped.)* |
| gcp-cloudsql | CKV_GCP_6 / AVD-GCP-0015 | google provider 5.x replaced `require_ssl` with `ssl_mode` (module default ENCRYPTED_ONLY); both scanners track the removed attribute. *(Re-justified when MS-9 shipped.)* |
| gcp-cloudsql | CKV_GCP_60 / AVD-GCP-0017 (public-address rule) | Public IP is the sandbox access path; the `private_network` option ships and drops it entirely. *(Re-justified when MS-9 shipped.)* |
| gcp-cloudsql | CKV_GCP_79 | Major-version bump is destructive for existing instances; version is variable-driven. |
| gcp-gce | ~~CKV_GCP_39 + AVD-GCP-0041/0045 (shielded) + AVD-GCP-0031 (public IP, tfsec)~~ | **REMOVED by MS-12 and still gone** — module defaults shielded ON / no public IP. |
| gcp-gce | CKV_GCP_32, CKV_GCP_38, CKV_GCP_40 / AVD-GCP-0030, AVD-GCP-0033 | **TOOL LIMITATION (re-added by MOD, 2026-07-13)**: ANY expression on `desired_status` (the Option-A power state) makes checkov/tfsec drop the resource's evaluated variable defaults, un-proving checks that genuinely pass. The MODSEED secure module defaults are UNCHANGED and the committed terraform test asserts those renders. MS-12 had these at zero (bare 19/0) before the power attribute existed. |
| gcp-gcs | CKV_GCP_62 | Bucket access logging needs a log bucket; demo posture, opt-in candidate. |
| gcp-gcs | AVD-GCP-0066 | CMEK: platform-managed encryption is the sandbox posture. |
| gcp-gke | CKV_GCP_12 / AVD-GCP-0056 | Network policy: sandbox cluster posture; enterprise hardening candidate. |
| gcp-gke | CKV_GCP_20 / AVD-GCP-0061 | Master authorized networks would cut the demo's kubectl access path. |
| gcp-gke | CKV_GCP_21 / AVD-GCP-0051 | Cluster resource labels: candidate; node labels already set. |
| gcp-gke | CKV_GCP_23 / AVD-GCP-0049 | Alias IP / VPC-native requires `ip_allocation_policy`: plan-shape change for existing clusters. |
| gcp-gke | CKV_GCP_25 + CKV_GCP_64 / AVD-GCP-0059 | Private cluster/nodes: sandbox posture (public endpoint demo). |
| gcp-gke | CKV_GCP_61 | VPC flow logs + intranode visibility: cost posture. |
| gcp-gke | CKV_GCP_65 | Google Groups RBAC needs Workspace group setup outside the sandbox. |
| gcp-gke | CKV_GCP_66 | Binary authorization: no image-signing pipeline exists in the sandbox. |
| gcp-gke | CKV_GCP_68 | Secure boot for shielded nodes: enterprise hardening candidate. |
| gcp-gke | CKV_GCP_69 ×2 / AVD-GCP-0057 ×2 | GKE metadata server / workload metadata requires workload identity: plan-shape change. |
| gcp-gke | CKV_GCP_70 | Release channel: pinned-version posture; owner-controlled upgrades. |
| gcp-gke | AVD-GCP-0047 | PodSecurityPolicy is removed in current Kubernetes; legacy rule. |
| gcp-gke | AVD-GCP-0048 | Legacy metadata endpoints are disabled by GKE since 1.12; explicit metadata would touch `node_config`. |
| gcp-gke | AVD-GCP-0050 | Dedicated node SA: sandbox uses the default compute SA; enterprise candidate. |
| gcp-gke | AVD-GCP-0054 | Image type defaults to COS containerd in current GKE. |
| gcp-vpc | AVD-GCP-0029 ×2 | Subnet flow logs: cost posture; opt-in candidate (MS-1 ships NAT + internal firewall). |
| gcp-kms | CKV_GCP_43 | `rotation_period` is derived from `var.rotation_days` (default 90d); checkov cannot evaluate the expression — the `_gcp_kms_policy` plan-JSON check enforces rotation at approval time (stronger: it sees the real plan). |
| gcp-kms | CKV_GCP_82 | `lifecycle prevent_destroy` would break the governed destroy flow; ring permanence is stated on the destroy card (MS-6). |


_Legend: [x] done · [~] partial/scaffolded · [ ] pending._
