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

## O. Tests (real, green) — expanded in Phase 6 (2026-07-05); **284 backend after Phase 7**
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
- [~] **Phase 1 — Trustworthy** (S0 S1 S2 S3 S4 S5 · A1+B7 A2 A4 A5 · B5 B6 · U4 · honesty
      labels · O2 C1 D1 D4). Exit gate: two orgs isolated in API+UI; no self-approve in prod;
      exactly one apply under concurrent approve; reveal gated+audited; no dishonest surface.
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
