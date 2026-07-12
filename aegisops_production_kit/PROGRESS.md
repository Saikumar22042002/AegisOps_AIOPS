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

Per-item status lives in the **`FIX.md §8` execution checklist** (the single progress tracker);
this section mirrors phase-level status only.

_Legend: [x] done · [~] partial/scaffolded · [ ] pending._
