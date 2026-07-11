# 03 — AWS operations (per-resource deep breakdown)

[← back to index](../../ANALYSIS.md) · Related: [04 Azure](04_azure_operations.md) · [05 GCP](05_gcp_operations.md) · [11 I/O reference](11_io_reference.md)

The real matrix comes from `agents/templates.py:TEMPLATES` + `_SYNONYMS`, `agents/params.py:PARAMS`, `schemas/workflows.py`, and the `infra/terraform-workspaces/*` modules. For AWS the supported resources are **EC2, S3, RDS, VPC, EKS**.

## 3.0 Supported matrix (AWS)

| Resource | Template key | Module dir | create | read | modify | destroy | Apply-verified? |
|----------|--------------|-----------|:---:|:---:|:---:|:---:|---|
| EC2 | `aws.ec2` | `aws-ec2` | ✅ | ✅ | ✅ (SG ports) | ✅ | apply (per code + fixtures/PROGRESS) |
| S3 | `aws.s3` | `aws-s3` | ✅ | ✅ (count) | ❌ | ✅ | apply |
| RDS | `aws.rds` | `aws-rds` | ✅ | ✅ (count) | ❌ | ✅ | plan |
| VPC | `aws.vpc` | `aws-vpc` | ✅ | ✅ (count) | ❌ | ✅ | plan |
| EKS | `aws.eks` | `eks-provision` | ✅ | ✅ (count) | ❌ | ✅ | plan |

- **read** for a *specific* resource resolves the AegisOps inventory (`_read_resource`); broad/account reads run `_read_path` (real SDK counts). EC2 read reconciles live via `boto3` describe.
- **modify** is implemented **only for compute** (`ec2`/`vm`), and only "add inbound TCP ports to the managed security group" — `_modify_resource` refuses any other resource type (`agents/cloudops.py:828`). There is **no** modify for S3/RDS/VPC/EKS.
- **destroy** works for any inventoried resource whose module still exists (`_destroy_resource`).
- Synonyms (`templates._SYNONYMS["aws"]`): vm/instance/server/compute→ec2, database/db/postgres/mysql/sql→rds, k8s/kubernetes/cluster→eks, bucket/blob/object_storage→s3, network→vpc.

---

## 3.1 AWS EC2 — create (the fully-worked path)

### Input (what the user must provide)
Decision-critical params asked if missing (`params.PARAMS["aws.ec2"]`, `required=True`): **name, instance_type, os, key_pair, allowed_cidr**. Everything else is defaulted and silently overridable: `region` (selected region), `vpc_id`/`subnet_id` (account default VPC/subnet), `root_volume_size` (AMI default = `null`), `root_volume_type` (`gp3`), `ingress_ports` (`[]`).

Validation (`schemas/workflows.py:AWSEC2Inputs`): `instance_type` must match `family+num+.+size` (e.g. `t3.micro`); `os` ∈ {amazon-linux-2023, ubuntu-22.04, ubuntu-24.04, windows-2022}; `allowed_cidr` via `_validate_cidr` (bare IP → `/32`, or `none`/`closed`→closed); `ingress_ports` coerced from `"8501,8502"` or list; `root_volume_type` allowlisted. The `key_pair` collection alias is transformed by `params._ec2_to_tf`: `"create"`/`"generate"`/… → `create_key_pair=True` + `key_name=<name>-key`; else an existing key name.

**Structured input card:** `emitter.params(request_payload)` → the "Required to proceed" card (`frontend/components/Workspace.tsx:ParamRequestCard`), listing only the missing required fields with help + choices.

### The create call chain, function by function
```
api/chat.py:chat
  → agents/runner.py:run_graph (thread_id=run_id; Langfuse trace opens)
    → agents/graph.py: router node (_timed)
        agents/router.py:router
          params.load_pending(session) ─ (none) →
          memory.classification_context(session)  ← recent turns
          llm.classify_json(system+catalog, msg)  → Gemini → {domain=cloudops, cloud=aws, resource=ec2, action=create, …}
          intent_guard.guard_classification(msg, cls)  ← deterministic overrides (no change for a clean create)
          _create_ticket → servicenow.create_service_request (if enabled)
          ContextGraph.create / set_intent  (Neo4j, best-effort)
    → _after_router → cloudops_plan
        agents/cloudops.py:cloudops_plan (action=create)
          resolve_cloud(state) → ("aws","named in request"|"UI selector")
          templates.select("aws","ec2") → WorkflowTemplate(aws.ec2, workspace=aws-ec2, schema=AWSEC2Inputs, policy=_ec2_policy)
          _extract_inputs(settings, template, msg):
              schemas.workflows.parse_freeform(msg)        ← "name=web-01, instance_type=t3.small" style
              llm.classify_json(extraction system, msg)    ← Gemini NL extraction (OS synonyms, "create" key)
          params.missing_required("aws.ec2", collected)
            ── if missing → emitter.params(request_payload) + emitter.token(summary_text)
                            params.save_pending(session, record)   ← Redis, 30-min TTL
                            RETURN needs_change=False, collecting=True   (NO PLAN)
          params.clear_pending(session)
          template.schema(**params.to_tf_vars(...)).model_dump()   ← Pydantic validate
            ── on ValidationError → drop bad field, save_pending, per-field re-ask, RETURN (no plan)
          (S3 only) bucket_taken precheck — n/a for EC2
          inventory.name_from_inputs → res_name;  tools/terraform.state_slug(res_name) → tf_state_ws = "res-<slug>"
          inventory.list_active(org) → duplicate active name? → refuse & re-ask (no plan)   (agents/cloudops.py:314)
          _availability(settings,"aws",region) → aws_tool.get_aws().ping() (STS)   ← Langfuse tool span
          runner = TerraformRunner("aws-ec2", settings, state_workspace=tf_state_ws)
          runner.init(on_line)          → terraform init  (+ workspace new res-<slug>)
          runner.plan(tf_vars, on_line) → terraform plan -out=aegisops-res-<slug>.tfplan
          runner.show_plan()            → terraform show -json (RAW capture) → _summarize_plan → {summary:{add,change,destroy}, diff[]}
          plan_guard.check_plan_actions("create", diff)   ← BLOCK if any delete/replace
          template.policy_fn(validated) → _ec2_policy (hardcoded checks)
          ContextGraph.set_workflow / add_step / add_evidence
          emitter.analysis + emitter.interrupt(payload)
          RETURN needs_change=True, approval_status="pending", plan_json, state_workspace, parsed_inputs=validated
    → _after_plan → approval node
        agents/approval.py:approval
          timing.start_step("approval", human_vs_auto="human")
          decision = interrupt(payload)    ← GRAPH PAUSES; run persisted awaiting_approval
```
On approve (`POST /approvals/{id}` → `run_graph(resume=Command(resume={decision,user,role,rationale}))`):
```
        approval resumes: records Approval row (immutable) + ContextGraph.add_approval
    → approval_decision == "execute"
        agents/execute.py:execute → agents/cloudops.py:cloudops_execute
          idempotency.make_key("tf-exec", run_id, "apply"); idempotency.claim(...)
          TerraformRunner(aws-ec2, state_workspace=state.state_workspace).apply(on_line)  ← terraform apply saved plan; streams console
              → runner.output() → non-sensitive outputs + sensitive_outputs=["private_key_pem"]
          idempotency.store_result; inventory.record_from_apply(state, template, outputs)
              → Postgres resources upsert (provider_id=instance_id, attrs=outputs, inputs=validated, state_workspace)
              → ContextGraph.add_resource (Neo4j resource↔run↔session)
          RETURN outcome={status:"applied", outputs, sensitive_outputs}
    → verify → finalize → servicenow_update → notify → END
        agents/finalize.py:verify
          asyncio.wait_for(_reconcile_checks, 30s):
              "Terraform outputs present" + (AWS) EC2 describe_instances(instance_id) state ∈ {pending,running}
          cards.success_card("ec2", outputs, inputs) → chat card (host/user/port/key ref, Reveal button)
          outcome.connection = {host, user, key_name, public_ip}
        finalize: resolution="Applied successfully."; ContextGraph.set_outcome + close
        servicenow_update: work note + close the SR/CR
        notify: persist Notification (+ SMTP if configured)
```

### Output (what the user sees / what's persisted)
- **Chat:** the "✅ Instance ready" card (`agents/cards.py`) with instance id, public address, login user, `ssh -i <key.pem> user@host` (port open to `allowed_cidr` only), and a **Reveal credential** button for `private_key_pem`.
- **Persisted:** `Run` (intent/domain/workflow/plan_json/input_json/outcome/status=completed), assistant `Message` (content=card, analysis, run_id), `run_steps` (router/cloudops_agent/policy_evaluation/planner/approval/execute/verify/finalize/…), `Approval` row (immutable), `Resource` inventory row, Neo4j context graph (closed), Langfuse trace (`cloudops-run`), Prometheus `aegisops_agent_runs_total{status="completed"}`. Terraform state in `terraform.tfstate.d/res-<slug>/`.
- **Credential:** `private_key_pem` is a **sensitive** TF output — excluded from `output()`/logs/DB/chat (`tools/terraform.py:output` filters `sensitive`), revealable exactly once via `POST /runs/{id}/credentials` (raw `terraform output -raw`, Redis NX one-shot).

### Failure paths
| Failure | Where handled | User sees |
|---------|---------------|-----------|
| Missing required param | `cloudops_plan` (missing_required) | "Required to proceed" card; pending saved; no plan |
| Invalid value (bad instance_type/os/cidr) | Pydantic `ValidationError` → `_invalid_fields` | Per-field "that didn't validate" re-ask; bad field dropped; no plan |
| Duplicate active name | `list_active` dup check (`cloudops.py:314`) | "You already have an active resource named X — pick another or destroy it first" |
| `terraform plan` fails (creds expired, IAM denied, quota, bad region) | `provider_errors.classify_provider_error` + `failure_message` | Plain-English what/why/next-step; raw trace in Logs; run `plan_failed` |
| Plan contains a destroy/replace (shared state) | `plan_guard.check_plan_actions("create", …)` | "Safety guard: a create must never tear anything down — halting" (blocked before approval) |
| `terraform apply` fails | `cloudops_execute` `TerraformError` | Classified message + `state_list()` leftover report; idempotency released; run `apply_failed` |
| Verify times out (30s) | `finalize.verify` `asyncio.TimeoutError` | "apply succeeded per Terraform; live status unconfirmed" (never hangs — N-01 fix) |
| Rejected at gate | `approval_decision` → finalize | "Plan rejected — no changes applied." |

**Module hardening** (`infra/terraform-workspaces/aws-ec2/main.tf`): IMDSv2 required, encrypted root volume, per-OS AMI data sources, dedicated day-2 SG, admin port (22/3389) opened only to `allowed_cidr` (never `0.0.0.0/0`), optional generated key pair (`tls_private_key` + `aws_key_pair`, private key sensitive). `ingress_ports` open to `0.0.0.0/0` (app-port exposure is intentional).

---

## 3.2 AWS S3 — create
Same chain as EC2 with two S3-specific steps: (1) required params **bucket_name** only (region/versioning/block_public defaulted); validation enforces lowercase 3–63 chars; (2) a **read-only `HeadBucket` precheck** (`aws_tool.bucket_taken`) before planning — a taken global name re-asks for a distinctive one (`cloudops.py:274`). Module: encrypted (AES256), versioning, public-access-block all on. Success card: name/ARN/region + console link + `aws s3 cp` hint. Verify reconciles via `bucket_taken`. No modify path.

## 3.3 AWS RDS — create (plan-verified)
Required: **identifier** only (engine=postgres, instance_class=db.t3.medium, allocated_storage=20, region defaulted). `instance_class` validated as `db.*.*`; `allocated_storage` 20–4096. Module: `manage_master_user_password=true` (AWS-managed in Secrets Manager), `storage_encrypted`, `publicly_accessible=false`, `skip_final_snapshot=true`. Success card: "Database ready" with endpoint + "credentials managed by the provider." No modify path; destroy works.

## 3.4 AWS VPC — create (plan-verified)
Required: **name** (cidr_block=10.0.0.0/16, az_count=3, enable_nat=true, region defaulted). Uses the official `terraform-aws-modules/vpc/aws ~> 5.8` with public+private subnets per AZ and NAT egress. Success card: "Network ready" with VPC id, CIDR, subnet ids. `_vpc_policy` checks multi-AZ (`az_count>=2`). No modify path.

## 3.5 AWS EKS — create (plan-verified)
Required: **cluster_name, vpc_id, subnet_ids** (kubernetes_version=1.29, instance_types=[m6i.xlarge], desired_size=3 defaulted). Reuses an existing VPC (no new VPC) via `terraform-aws-modules/eks/aws ~> 20.8`; private API endpoint only, secrets KMS encryption, IRSA. `_eks_policy` asserts multi-AZ from `len(subnet_ids)>=2`. This is the only module with a separate `variables.tf`/`outputs.tf` (dir `eks-provision`). No modify path.

## 3.6 AWS day-2: read / modify / destroy

**Read (specific)** — `_read_resource(target)` (`cloudops.py:601`): `inventory.resolve` (exact/substring/typed-descriptive) → if one match, `inventory.reconcile` (EC2: live `boto3` describe refreshes IPs/state/VPC/subnet; **⚠ blocking boto3 on the event loop**, `inventory.py:229`) → returns real recorded values (size, region, VPC, subnet, IPs, SG, key, open ports, status) + provenance from the graph. Not found → "I won't guess"; ambiguous → "which one?".

**Read (broad/account)** — `_read_path` (`cloudops.py:509`): determines clouds from the message (or resolved/all-configured), runs `_discover_aws` (running EC2 count + names, S3 buckets, RDS count, EKS count, VPC count), and appends the AegisOps inventory listing. One cloud failing never sinks the others (`provider_errors` classifies each).

**Modify** — `_modify_resource(target)` (`cloudops.py:804`): resolves the resource, refuses anything but ec2/vm, extracts ports (`_extract_ports` via Gemini + regex), merges into `ingress_ports`, re-validates, plans against the resource's **own** state workspace, `plan_guard.check_plan_actions("modify", …)` (blocks any replace/destroy = an in-place-only guarantee), then the approval gate → apply → inventory update.

**Destroy** — `_destroy_resource(target)` (`cloudops.py:674`): requires an explicitly destructive verb (belt-and-suspenders under the router mirror-guard), resolves the target from inventory (never param collection), refuses bulk, plans `-destroy` in the resource's own state workspace, `plan_guard.check_plan_actions("destroy", …)` (only deletes allowed), zero-destroy → "already empty", else approval gate → apply → `inventory.mark_destroyed` + graph mark.

## 3.7 Not implemented (AWS)
- **Modify** for S3/RDS/VPC/EKS (only compute SG-ports).
- **Read** does not surface RDS/EKS *details* by name (only account-level counts + the inventory record).
- No cost estimation (the design/UI mentions "$/mo within guardrail"; no code computes cost — the reasoning cards mention it only as static text where seeded).
- Policy checks are hardcoded `True` (`_ec2_policy` etc.) — not evaluated against the plan (see [09](09_problems.md)).
