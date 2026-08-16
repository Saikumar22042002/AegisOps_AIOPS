# Architecture Diagrams

> Eight Mermaid diagrams. 1–3 describe what exists (audited at `a974290`); 4–8 describe
> the target. Render on GitHub, VS Code Mermaid preview, or mermaid.live.

---

## 1. Current AegisOps Architecture

```mermaid
flowchart TB
  subgraph CH["Channels"]
    UI["Next.js SPA\nstore.ts + SSE"]
    TG["Telegram\nlong-poll"]
  end

  subgraph API["Control plane - FastAPI (stateless x2 workers)"]
    AUTH["Keycloak OIDC\nstrict tenancy + RBAC"]
    PREP["prepare_run\nmodel validate - limits - Run row"]
    APR_EP["POST /approvals\nfour-eyes core"]
    ART["runs + artifact tabs\norg-scoped 404s"]
  end

  subgraph GW["Gateway seam (GW-1)"]
    TRANS["Transport Protocol"]
    IDENT["identity.py\none-time codes -> Keycloak user"]
    STREAMER["stream.py\npreview edit ladder"]
  end

  subgraph GRAPH["LangGraph - 12 nodes, Postgres checkpointer, thread_id == run_id"]
    RT["router"]
    CO["cloudops_plan"]
    DO["devops_plan"]
    SRE["sre_analyze"]
    KN["knowledge"]
    GEN["general"]
    APV["approval - durable interrupt"]
    EXE["execute"]
    VER["verify"]
    FIN["finalize"]
    SNOW["servicenow_update"]
    NOT["notify"]
  end

  subgraph LLM["LLM layer (today)"]
    ALLM["agents/llm.py\nclassify_json - stream_answer"]
    SING["get_gemini singleton\ncontextvar run model"]
    REG["llm/registry\nvalidation only - 3 gemini ids"]
  end

  subgraph EXEC["Governed mutation"]
    TPL["templates.py\n20 modules + 1 alias + policy predicates"]
    PG_GUARD["plan_guard"]
    TF["TerraformRunner\nTF_WORKSPACE isolation - saved plans"]
    LOOP["exec_loop\nDAG - wires - deviation - MAX_STEPS 5"]
  end

  subgraph DATA["State"]
    PGDB[("Postgres + pgvector\nruns, steps, approvals, checkpoints")]
    RDS[("Redis\nevent streams, idempotency, heartbeats")]
    NEO[("Neo4j\nworld model, impact_of")]
  end

  subgraph OBS["Observability"]
    LF["Langfuse\ntrace_id == run_id"]
    PROM["OTel -> Prometheus/Grafana"]
  end

  UI --> PREP
  TG --> TRANS --> IDENT --> PREP
  PREP --> GRAPH
  RT --> CO & DO & SRE & KN & GEN
  CO --> APV --> EXE --> VER --> FIN --> SNOW --> NOT
  RT & CO & KN & GEN --> ALLM --> SING
  PREP -.->|"validate only, provider discarded"| REG
  CO --> TPL --> PG_GUARD --> TF
  CO --> LOOP --> TF
  GRAPH --> PGDB
  GRAPH --> RDS
  EXE --> NEO
  GRAPH -.-> LF
  API -.-> PROM
  APR_EP --> GRAPH
  STREAMER --> TG
```

---

## 2. Current Harness (the honest close-up)

```mermaid
flowchart LR
  subgraph AG["Agent nodes (single pass, no iteration)"]
    R2["router\nprompt-and-parse JSON"]
    C2["cloudops extract\nprompt-and-parse JSON"]
    G2["general/knowledge\nstream_answer"]
  end

  subgraph L2["agents/llm.py"]
    CJ["classify_json"]
    SA["stream_answer\nretry x3 on truncation"]
  end

  subgraph GEM["integrations/gemini.py"]
    SG["module singleton\nno timeout - no gen params"]
    CV["contextvar _run_model\nset per run in _drive"]
    TOOLS_UNUSED["tools= param exists\nNEVER populated"]
  end

  subgraph SEAM["integrations/llm (U3 seam)"]
    BASE["LLMProvider Protocol"]
    REG2["registry: 1 provider, 3 model ids"]
    GP["GeminiProvider\nagenerate/astream: ZERO callers"]
  end

  subgraph INV["investigation.py (bones without a brain)"]
    FRZ["frozen read-only registry\n19-marker mutation denylist"]
    BUD["MAX_CALLS 8 - spawn shares budget"]
    NODIR["no LLM director\n1 hardcoded call from sre.py"]
  end

  R2 & C2 --> CJ --> SG
  G2 --> SA --> SG
  CV --> SG
  API_IN["POST /chat body.model"] --> REG2
  REG2 -->|"resolved_model string only"| CV
  REG2 -.->|"provider object discarded"| GP
  SG -->|"usage"| LFX["Langfuse generations\n(no ledger, embeddings unrecorded)"]
  FRZ --- BUD --- NODIR
```

Key facts the diagram encodes: model selection is real but string-only (the provider
object is discarded); all inference flows through one singleton; native tool-calling
is absent; the read-only tool registry has no director; tokens go to Langfuse only.

---

## 3. Waku Harness

```mermaid
flowchart TB
  subgraph GWY["Gateways"]
    CLI["cli"]
    VOICE["voice"]
    TGW["telegram"]
    DIS["discord"]
    DASH["dashboard"]
  end

  RESP["Waku.respond()\none turn"]

  subgraph TRIAGE["optional triage graph (fail-open)"]
    CLS["small-model classify"]
    QK["quick_reply - small model"]
    FULLN["full_agent node"]
  end

  subgraph CTX["Session.build_system - per turn"]
    SOUL["SOUL.md persona"]
    TIME["local time + tz"]
    IDENT2["own model identity"]
    GATE["retrieval gate\ncheap model - fails open"]
    SKILLS["matched SKILL.md bodies"]
  end

  subgraph LOOP2["run_loop - max 10 iterations"]
    LLMCALL["llm(messages, tools)\nstream w/ fallback"]
    DECIDE{"tool_use?"}
    EXECT["registry.execute\nerrors become observations"]
    APPEND["append results as user msg"]
  end

  subgraph PROV["Providers - 2 wire formats, 11 entries"]
    ANTH["anthropic-native\nAnthropic Kimi GLM MiniMax"]
    OAIC["openai-compat bridge ~110 lines\nOpenAI Gemini DeepSeek OpenRouter xAI ..."]
  end

  subgraph MEM["Memory tiers"]
    RAWL["chat_log"]
    FACTS["facts + FTS"]
    EPIS["episodes"]
    PROC["SKILL.md files"]
    CONS["consolidation\nevery 6 exchanges - small model"]
  end

  OBSV["observer compose\ngateway UI + JSONL trace + OTel + usage.jsonl ledger"]

  GWY --> RESP --> TRIAGE
  CLS --> QK
  CLS --> FULLN --> CTX --> LOOP2
  LLMCALL --> DECIDE
  DECIDE -->|yes| EXECT --> APPEND --> LLMCALL
  DECIDE -->|no| REPLY["reply + tools-used fold-in"]
  LLMCALL --- PROV
  GATE --- MEM
  REPLY --> CONS --> FACTS & EPIS
  LOOP2 -.-> OBSV
```

---

## 4. Proposed AegisOps Architecture

```mermaid
flowchart TB
  subgraph CH4["Channels"]
    WEB4["Web SPA"]
    TG4["Telegram"]
    SLK4["Slack / Teams (new transports)"]
    HOOK4["Alert webhooks -> incident runs"]
  end

  subgraph CP4["Control plane - FastAPI"]
    ADM["prepare_run - single admission\nOIDC, tenancy, RBAC, limits, RoutePlan pin, budget"]
    APRC["approval core\nfour-eyes + click-time re-check"]
    SET["Settings/Models admin\nbindings + eval gate"]
  end

  subgraph AP4["Agent plane - Intelligent Shell (no mutation authority)"]
    KERN["AgentKernel\nbounded loop - budgets incl cost - stuck detector - checkpoints"]
    PLN["planner -> GoalDAG draft"]
    INV4["investigator - INV loop\nfrozen read-only registry"]
    OTH4["router / sre-triage / knowledge / general"]
    CTX4["Context Engine\nrecipes - per-iteration - gate - consolidation proposals"]
  end

  subgraph PL4["Provider layer - app/llm"]
    SVC["service.generate(purpose)"]
    RTR["ModelRouter + Capability Registry"]
    EXECU["ResilientExecutor\nretry - breaker - fallback - budget"]
    ADPT["adapters: anthropic / openai-compat / google / bedrock / azure / litellm*"]
    LEDG["llm_usage ledger"]
  end

  subgraph GC4["Governed core - Execution plane (deterministic)"]
    CMP["compile_goal_dag\ncatalog - wiring - guard - compensation closure - locks"]
    APRV4["Approval service\nartifact: plans+policy+cost+impact+verify+rollback"]
    ENG["WorkflowEngine\nwaves - idempotency - windows - cancel at boundary"]
    EXTF["Terraform executor"]
    EXK8["K8s executor\ndry-run diff / rollout"]
    EXD2["Day-2 verb registry\ngoverned SDK actions"]
  end

  subgraph ST4["State and world"]
    PG4[("Postgres: runs/steps/approvals/ledger/bindings/prompts/checkpoints")]
    RD4[("Redis: streams/locks/idempotency/heartbeats")]
    NEO4[("Neo4j world model + drift reconciler")]
  end

  subgraph OB4["Observability and quality"]
    LF4["Langfuse trace==run"]
    PR4["OTel -> Prom/Grafana"]
    FLOW["Live flow console + stalled-step tell"]
    EVAL["CI eval gate: dataset + judge + prompt versions"]
  end

  CH4 --> ADM --> AP4
  KERN --> SVC --> RTR --> EXECU --> ADPT
  EXECU --> LEDG
  PLN -->|"data only"| CMP --> APRV4 --> ENG
  ENG --> EXTF & EXK8 & EXD2
  INV4 -->|"reads"| NEO4
  ENG --> PG4 & NEO4
  AP4 --> RD4
  APRC --> APRV4
  SET --> RTR
  GC4 -.-> LF4 & PR4
  ENG --> FLOW
  EVAL -.->|"gates"| SET
```

---

## 5. Proposed Agent Harness (kernel + provider layer close-up)

```mermaid
flowchart TB
  subgraph SPEC["AgentSpec"]
    PUR["purpose string\n(the ONLY model coupling)"]
    TPOL["tool policy\nREAD_ONLY_FROZEN / GOVERNED_PROPOSE"]
    BUDG["budgets: iterations, calls, cost, wall-clock"]
  end

  subgraph LOOP5["kernel loop - per iteration"]
    B1["budget check"]
    B2["context assemble (recipe, per-iteration)"]
    B3["llm.generate(purpose, messages, tools)"]
    B4{"tool calls?"}
    B5["registry.execute via middleware\ntenancy-rbac-rate-timeout-redact-audit"]
    B6["observations appended (errors included)"]
    B7["checkpoint iteration"]
    B8["reply / honest partial"]
  end

  subgraph PL5["app/llm"]
    RT5["router: purpose -> RoutePlan\nbindings DB over models.yaml"]
    CAP["capability registry\nneeds checked at config time"]
    RES["resilient executor\nretry/jitter - breaker(redis) - fallback hops visible"]
    AD5["adapters (only SDK importers)\ncanonical types in/out"]
    STRN["stream normalizer\nTextDelta/ToolCall*/Thinking/StreamDone"]
    LED5["ledger + budgets\ntokens ground truth"]
  end

  EMIT["run event bus (Redis streams)\ntoken/step/agent_llm/agent_tool events"]

  SPEC --> LOOP5
  B1 --> B2 --> B3 --> B4
  B4 -->|yes| B5 --> B6 --> B7 --> B1
  B4 -->|no| B8
  B3 --> RT5 --> CAP
  RT5 --> RES --> AD5
  AD5 --> STRN --> EMIT
  RES --> LED5
  B5 --> EMIT
```

---

## 6. End-to-End Request Flow (provisioning with approval)

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant FE as Web UI
  participant API as prepare_run
  participant RT as router agent
  participant PL as planner agent
  participant LLM as app/llm service
  participant CMP as compile_goal_dag
  participant AP as approval interrupt
  actor H as Approver
  participant EN as WorkflowEngine
  participant TF as Terraform executor
  participant VF as verify

  U->>FE: "create VPC and a VM inside it"
  FE->>API: POST /chat (message, model?)
  API->>API: OIDC + tenancy + RBAC + limits + RoutePlan pin
  API-->>FE: SSE stream opens (run event)
  API->>RT: classify
  RT->>LLM: generate(purpose=router)
  LLM-->>RT: domain=cloudops action=create (structured)
  RT->>PL: goal
  PL->>LLM: generate(purpose=planner, tools=propose)
  LLM-->>PL: GoalDAG draft (s1 vpc, s2 vm wired)
  PL->>CMP: draft (data only)
  CMP->>CMP: catalog + wiring + guard + compensation + locks
  CMP->>TF: plan per step (isolated workspaces)
  TF-->>CMP: diffs + policy predicate results
  CMP->>AP: approval artifact (plans+cost+impact+verify+rollback)
  AP-->>FE: interrupt event (run awaiting_approval)
  Note over AP: durable checkpoint - survives restarts
  H->>API: POST /approvals (web or gateway button)
  API->>API: RBAC + four-eyes + state + in-flight lock
  API->>EN: resume from checkpoint (any worker)
  EN->>TF: wave 1: apply s1 (idempotency claimed)
  TF-->>FE: console lines stream
  EN->>TF: wave 2: apply s2 (wires resolved from s1 outputs)
  EN->>VF: verify steps
  VF-->>EN: EvidenceCards
  EN-->>FE: done (outcome, served_by badges, trace link)
```

---

## 7. CloudOps Execution Flow (engine step lifecycle + saga)

```mermaid
flowchart TB
  START7(["approved Workflow"]) --> WIN{"inside change window?"}
  WIN -->|no| SCHED["status: scheduled\nreconciler launches at window"]
  SCHED --> WIN
  WIN -->|yes| READY["compute ready set\n(deps done + locks free)"]
  READY --> WAVE["run wave (parallel, disjoint outputs)"]

  subgraph STEP["per step"]
    IDEM["claim idempotency key\nlost -> stored result / wait / abort"]
    PRE{"preconditions hold?"}
    PLAN7["plan in step workspace"]
    GUARD7["plan_guard + policy predicates"]
    APPLY7["apply (console streamed, heartbeat)"]
    VERIFY7{"verify -> evidence ok?"}
    REC["record: inventory + world model + run_steps (same txn)"]
  end

  WAVE --> IDEM --> PRE
  PRE -->|no| DEV["DEVIATION\nre-approval interrupt (was/now diff)"]
  PRE -->|yes| PLAN7 --> GUARD7 --> APPLY7 --> VERIFY7
  VERIFY7 -->|yes| REC --> MORE{"more steps?"}
  VERIFY7 -->|no| RETRY{"retriable class + budget left?"}
  RETRY -->|yes| APPLY7
  RETRY -->|no| POLICY{"on_failure policy"}
  DEV -->|approved| PLAN7
  DEV -->|rejected| HALT["honest partial outcome"]
  MORE -->|yes| READY
  MORE -->|no| DONE(["completed + evidence"])
  POLICY -->|halt| HALT
  POLICY -->|rollback| SAGA["compensate completed steps\nreverse order, each planned+guarded+verified"]
  SAGA -->|all ok| RB(["rolled_back (truthful report)"])
  SAGA -->|compensation fails| FREEZE["FREEZE + page\nno second-order automation"]
  CANCEL["cancel requested"] -.->|"only at step boundary, never mid-apply"| READY
```

---

## 8. Multi-Agent Flow (bounded fan-out, typed blackboard)

```mermaid
flowchart TB
  GOAL["user goal / alert"] --> RT8["router (fast model)\nclassify only - code routes"]

  subgraph BB["typed run state (blackboard - no prose handoffs)"]
    ST8["classification | evidence | draft DAG | critique | artifact"]
  end

  RT8 --> ST8

  subgraph FAN["read-only fan-out (shared budget pool, depth cap 1)"]
    I1["investigator A\nhypothesis: deploy regression"]
    I2["investigator B\nhypothesis: capacity"]
    W8["world model reads\nimpact_of, inventory"]
  end

  ST8 --> FAN --> ST8

  PL8["planner (flagship, high reasoning)\nGoalDAG draft from evidence"] 
  CR8["critic (cheap)\nmissing deps? rollback gaps?\nadvisory notes only"]

  ST8 --> PL8 --> ST8
  ST8 --> CR8 --> ST8

  CMP8["compile_goal_dag (pure code)\nrefusal or Workflow + artifact"]
  ST8 --> CMP8

  HUM8{"human approval\n(four-eyes in prod)"}
  CMP8 --> HUM8
  HUM8 -->|approved| ENG8["WorkflowEngine (singular, deterministic)"]
  HUM8 -->|rejected| REP8["honest report"]
  ENG8 --> EV8["evidence + postmortem draft\nconsolidation -> memory PROPOSALS (human-accepted)"]

  NOTE8["rules: LLMs never route control flow - code does\nmutation never fans out - one engine per run\nsubagent spend ledgered (agent_kind)"]
```
