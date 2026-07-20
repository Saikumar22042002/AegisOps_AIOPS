// Seed data ported verbatim from DESIGN_REFERENCE.logic.js. In M2 these drive the
// pixel-exact UI; from M3 the same SHAPES are returned by the real backend so components
// bind unchanged, and these literals are replaced by live API data.

import { cloudColor, modelColor } from "./colors";
import type { ApprovalState } from "./types";

export interface Opt {
  label: string;
  sub?: string;
  dot?: string;
}

// CLN-2: orgOptions/envOptions/regionOptions removed — zero references anywhere. The org
// comes from the authenticated principal (S0), the env is fixed to Production in the store
// (no picker ships — a four-eyes-relevant fact documented at STAB P0-3), and the region
// selector never rendered a menu. Re-adding a picker means wiring real state, not a list.

// U4: "Auto (ask me)" is the default — it maps to cloud=null on the wire, so an ambiguous
// request (no cloud named) triggers the clarifying question instead of silently defaulting to AWS.
export const AUTO_CLOUD = "Auto (ask me)";

/** Selector label → wire value: Auto (or anything not a real cloud selection) sends null. */
export function cloudToWire(cloud: string): string | null {
  return !cloud || cloud.startsWith("Auto") ? null : cloud;
}

export const cloudOptions: Opt[] = [
  { label: AUTO_CLOUD, sub: "clarify per request" },
  { label: "AWS", sub: "12 accounts" },
  { label: "Azure", sub: "4 subscriptions" },
  { label: "GCP", sub: "2 projects" },
  { label: "Kubernetes", sub: "8 clusters" },
  { label: "VMware", sub: "vSphere" },
].map((o) => ({ ...o, dot: cloudColor(o.label) }));

// U3: the model menu lists exactly the models the backend serves (Google Gemini today) and
// sends the raw model id as `model` on /chat. The backend validates it against the same
// catalog (GET /models) and rejects anything else with a 400 — no advertising providers we
// can't run. Keep this in sync with `available_models` in the backend registry.
export const modelOptions: Opt[] = [
  { label: "gemini-3.5-flash", sub: "Google Gemini · default" },
  { label: "gemini-flash-latest", sub: "Google Gemini · latest flash" },
  { label: "gemini-2.5-flash", sub: "Google Gemini · GA fallback" },
].map((o) => ({ ...o, dot: modelColor(o.label) }));

export const roleOptions: Opt[] = [
  { label: "Platform Admin", sub: "Full control" },
  { label: "Org Admin", sub: "Org-wide" },
  { label: "Cloud Architect", sub: "Design + plan" },
  { label: "DevOps Engineer", sub: "Deploy + run" },
  { label: "SRE", sub: "Incidents + reliability" },
  { label: "Developer", sub: "Initiate requests" },
  { label: "Auditor", sub: "Read + audit" },
  { label: "Read Only", sub: "View only" },
];

export const notifItems = [
  { title: "Approval requested · EKS production plan", time: "2m", color: "var(--amber)" },
  { title: "INC-2291 checkout latency · assigned to you", time: "14m", color: "var(--red)" },
  { title: "deploy orders-api v4.2.1 succeeded", time: "1h", color: "var(--green)" },
  { title: "Drift detected · data-lakehouse staging", time: "3h", color: "var(--cyan)" },
];

export interface ModuleStat {
  label: string;
  value: string;
  delta: string;
  deltaColor: string;
}
export interface ModuleRow {
  dot: string;
  name: string;
  meta: string;
  value: string;
}
export interface ModuleMeta {
  eyebrow: string;
  title: string;
  icon: string;
  desc: string;
  listTitle: string;
  stats: ModuleStat[];
  rows: ModuleRow[];
}

export const moduleMeta: Record<string, ModuleMeta> = {
  projects: {
    eyebrow: "Workspaces",
    title: "Projects",
    icon: "P",
    desc: "Every project carries its own infrastructure, conversations, memory and governance. Open one to work inside its context — Terraform, resources and incidents all surface here.",
    listTitle: "Active projects",
    stats: [
      { label: "Projects", value: "12", delta: "+2 this quarter", deltaColor: "var(--green)" },
      { label: "Cloud accounts", value: "7", delta: "AWS · Azure · GCP", deltaColor: "var(--text-3)" },
      { label: "Open conversations", value: "34", delta: "6 awaiting you", deltaColor: "var(--amber)" },
      { label: "Monthly spend", value: "$48.2k", delta: "−4.1% vs last mo", deltaColor: "var(--green)" },
    ],
    rows: [
      { dot: "var(--red)", name: "payments-platform", meta: "Production · EKS provisioning in progress", value: "$18.4k/mo" },
      { dot: "var(--green)", name: "orders-api", meta: "Production · healthy · 8 replicas", value: "$9.1k/mo" },
      { dot: "var(--amber)", name: "data-lakehouse", meta: "Staging · drift detected on 2 resources", value: "$12.6k/mo" },
      { dot: "var(--green)", name: "identity-service", meta: "Production · healthy", value: "$5.3k/mo" },
    ],
  },
  infrastructure: {
    eyebrow: "CloudOps · multi-cloud",
    title: "Infrastructure",
    icon: "I",
    desc: "Every resource the agents discover across AWS, Azure, GCP, Kubernetes and VMware — explorable as a live graph. Open any resource to inspect, plan, or remediate it conversationally.",
    listTitle: "Tracked resources",
    stats: [
      { label: "Resources", value: "2,418", delta: "+312 (90d)", deltaColor: "var(--green)" },
      { label: "Clusters", value: "8", delta: "EKS · AKS · GKE", deltaColor: "var(--text-3)" },
      { label: "Drift detected", value: "2", delta: "data-lakehouse", deltaColor: "var(--amber)" },
      { label: "Monthly spend", value: "$48.2k", delta: "−4.1% MoM", deltaColor: "var(--green)" },
    ],
    rows: [
      { dot: "var(--cyan)", name: "payments-prod-use1", meta: "EKS cluster · us-east-1 · 3 AZ", value: "healthy" },
      { dot: "var(--green)", name: "vpc-0a91c4f2", meta: "Prod VPC · 6 subnets · 2 NAT", value: "healthy" },
      { dot: "var(--amber)", name: "data-lakehouse-rds", meta: "Aurora PostgreSQL · drift", value: "drift" },
      { dot: "var(--green)", name: "orders-api", meta: "ECS service · 8 tasks", value: "healthy" },
    ],
  },
  incidents: {
    eyebrow: "SRE · incident management",
    title: "Incidents",
    icon: "!",
    desc: "AI-triaged incidents correlated with deploys, metrics and traces. Every incident opens a context graph with RCA, timeline and ServiceNow linkage — investigate it conversationally.",
    listTitle: "Active & recent incidents",
    stats: [
      { label: "Open", value: "1", delta: "P3 · checkout latency", deltaColor: "var(--amber)" },
      { label: "MTTR (30d)", value: "14m", delta: "−6m vs prev", deltaColor: "var(--green)" },
      { label: "Deploys today", value: "23", delta: "22 succeeded", deltaColor: "var(--green)" },
      { label: "False positives", value: "6%", delta: "AI-triaged", deltaColor: "var(--text-3)" },
    ],
    rows: [
      { dot: "var(--amber)", name: "INC-2291 · checkout latency", meta: "P3 · correlating with 14:20 deploy · SR linked CHG0040021", value: "open" },
      { dot: "var(--green)", name: "INC-2287 · eu-west-1 pod restarts", meta: "5 pods rescheduled · auto-resolved", value: "resolved" },
      { dot: "var(--green)", name: "INC-2280 · RDS failover staging", meta: "RCA published · runbook updated", value: "resolved" },
      { dot: "var(--cyan)", name: "deploy orders-api v4.2.1", meta: "GitHub Actions · prod · no incident", value: "succeeded" },
    ],
  },
  knowledge: {
    eyebrow: "Semantic search",
    title: "Knowledge Center",
    icon: "K",
    desc: "Runbooks, RCAs, architecture docs and conversation summaries — searchable semantically and cited automatically inside the AI Workspace.",
    listTitle: "Recently used",
    stats: [
      { label: "Documents", value: "1,284", delta: "+47 this month", deltaColor: "var(--green)" },
      { label: "Runbooks", value: "96", delta: "12 auto-generated", deltaColor: "var(--text-3)" },
      { label: "RCAs", value: "38", delta: "linked to incidents", deltaColor: "var(--text-3)" },
      { label: "Avg relevance", value: "91%", delta: "citation accuracy", deltaColor: "var(--green)" },
    ],
    rows: [
      { dot: "var(--accent-3)", name: "EKS Production Hardening", meta: "Runbook · cited 94% relevance", value: "updated 2d" },
      { dot: "var(--cyan)", name: "Payments Platform Architecture v3", meta: "Design doc", value: "updated 1w" },
      { dot: "var(--green)", name: "RCA: checkout outage 06-19", meta: "Root cause analysis", value: "8d ago" },
      { dot: "var(--violet)", name: "Incident response playbook", meta: "SOP · on-call", value: "updated 3w" },
    ],
  },
  analytics: {
    eyebrow: "Executive view",
    title: "Analytics",
    icon: "A",
    desc: "Engineering, SRE, DevOps and cloud KPIs in one place. Every chart is explainable — ask the AI why a number moved and get a grounded answer.",
    listTitle: "Key metrics",
    stats: [
      { label: "Cloud spend (mo)", value: "$48.2k", delta: "−4.1% MoM", deltaColor: "var(--green)" },
      { label: "Deploy frequency", value: "23/day", delta: "+18% QoQ", deltaColor: "var(--green)" },
      { label: "Change failure", value: "2.1%", delta: "elite tier", deltaColor: "var(--green)" },
      { label: "Agent success", value: "97.4%", delta: "across 1.2k runs", deltaColor: "var(--text-3)" },
    ],
    rows: [
      { dot: "var(--green)", name: "Infrastructure growth", meta: "+312 resources tracked (90d)", value: "↑ 12%" },
      { dot: "var(--amber)", name: "Cost by environment", meta: "Production 64% · Staging 21%", value: "$48.2k" },
      { dot: "var(--cyan)", name: "MTBF", meta: "Mean time between failures", value: "41 days" },
      { dot: "var(--accent-3)", name: "AI productivity", meta: "Tasks completed via AI", value: "8,140" },
    ],
  },
  admin: {
    eyebrow: "Governance & identity",
    title: "Administration",
    icon: "S",
    desc: "Organizations, RBAC, approval policies, audit and MCP servers. Governance recommendations are surfaced by the AI rather than buried in config screens.",
    listTitle: "Governance overview",
    stats: [
      { label: "Members", value: "184", delta: "12 teams", deltaColor: "var(--text-3)" },
      { label: "Approval policies", value: "14", delta: "3 require review", deltaColor: "var(--amber)" },
      { label: "Audit events (24h)", value: "6,402", delta: "all signed", deltaColor: "var(--green)" },
      { label: "MCP servers", value: "9", delta: "9 healthy", deltaColor: "var(--green)" },
    ],
    rows: [
      { dot: "var(--green)", name: "prod-change-control", meta: "Approval policy · 6 checks", value: "active" },
      { dot: "var(--amber)", name: "Permission review", meta: "3 over-privileged roles flagged by AI", value: "review" },
      { dot: "var(--green)", name: "Keycloak SSO", meta: "OIDC · Azure AD · MFA enforced", value: "healthy" },
      { dot: "var(--cyan)", name: "terraform-mcp", meta: "MCP server · 142 tools · 38ms", value: "healthy" },
    ],
  },
  settings: {
    eyebrow: "Personal",
    title: "Profile & Settings",
    icon: "⚙",
    desc: "Your preferences, notification rules, connected accounts, and personal guardrails. AegisOps adapts its behavior to the defaults you set here.",
    listTitle: "Preferences",
    stats: [
      { label: "Theme", value: "Dark", delta: "system default", deltaColor: "var(--text-3)" },
      { label: "Approval mode", value: "Required", delta: "for production", deltaColor: "var(--amber)" },
      { label: "Cost alert", value: "$500", delta: "per change", deltaColor: "var(--text-3)" },
      { label: "Sessions", value: "3", delta: "active devices", deltaColor: "var(--green)" },
    ],
    rows: [
      { dot: "var(--green)", name: "Notification rules", meta: "Slack · email · approval requests", value: "configured" },
      { dot: "var(--cyan)", name: "Connected accounts", meta: "GitHub · AWS · Azure AD", value: "3 linked" },
      { dot: "var(--accent-3)", name: "Default agent mode", meta: "Approval required in production", value: "enabled" },
      { dot: "var(--amber)", name: "Cost guardrail", meta: "Flag changes above $500/mo", value: "$500" },
    ],
  },
};

export const integrations = [
  { name: "Keycloak", cat: "Identity · SSO/SAML", mark: "K", color: "var(--accent-3)", status: "connected", statusColor: "var(--green)" },
  { name: "LangGraph", cat: "Agent orchestration", mark: "LG", color: "var(--accent-2)", status: "active", statusColor: "var(--green)" },
  { name: "Langfuse", cat: "LLM observability", mark: "Lf", color: "var(--cyan)", status: "tracing", statusColor: "var(--green)" },
  { name: "OpenTelemetry", cat: "Traces · metrics", mark: "OT", color: "var(--violet)", status: "connected", statusColor: "var(--green)" },
  { name: "Prometheus", cat: "Metrics", mark: "Pr", color: "var(--amber)", status: "scraping", statusColor: "var(--green)" },
  { name: "Grafana", cat: "Dashboards", mark: "Gf", color: "var(--amber)", status: "connected", statusColor: "var(--green)" },
  { name: "PostgreSQL", cat: "Primary datastore", mark: "Pg", color: "var(--cyan)", status: "healthy", statusColor: "var(--green)" },
  { name: "Redis", cat: "Cache · queues", mark: "Rd", color: "var(--red)", status: "healthy", statusColor: "var(--green)" },
  { name: "Neo4j", cat: "Context graph", mark: "N4", color: "var(--green)", status: "connected", statusColor: "var(--green)" },
  { name: "Terraform", cat: "Provisioning", mark: "Tf", color: "var(--accent-2)", status: "connected", statusColor: "var(--green)" },
  { name: "Ansible", cat: "Configuration", mark: "An", color: "var(--red)", status: "connected", statusColor: "var(--green)" },
  { name: "GitHub", cat: "SCM · Actions", mark: "Gh", color: "var(--text-2)", status: "connected", statusColor: "var(--green)" },
  { name: "ServiceNow", cat: "ITSM · SR/CR/INC", mark: "SN", color: "var(--green)", status: "syncing", statusColor: "var(--green)" },
];

export const seedTimeline = [
  { label: "Understood intent", detail: "provision EKS · reuse VPC · 3 AZ", time: "0.3s" },
  { label: "Retrieved memory", detail: "approved module · naming convention", time: "0.2s" },
  { label: "Queried AWS", detail: "us-east-1 · found prod VPC", time: "1.2s" },
  { label: "Searched knowledge", detail: "2 runbooks · 1 design doc", time: "0.8s" },
  { label: "Selected module", detail: "terraform-aws-eks v20.8", time: "0.1s" },
  { label: "Checked policies", detail: "6 of 6 passed", time: "0.4s" },
  { label: "Ran terraform plan", detail: "+14 ~2 -0", time: "3.4s" },
  { label: "Estimated cost", detail: "+$312/mo · within guardrail", time: "0.2s" },
  { label: "Composed artifacts", detail: "plan + cost analysis", time: "0.3s" },
  { label: "Awaiting approval", detail: "production change", time: "···" },
];

export const traceSpans = [
  { name: "intent.classify", dur: "0.3s", dot: "var(--green)", indent: "0px", tokens: "1.2k tok" },
  { name: "agent.route", dur: "0.1s", dot: "var(--green)", indent: "0px", tokens: "" },
  { name: "workflow.plan", dur: "0.6s", dot: "var(--green)", indent: "12px", tokens: "2.1k tok" },
  { name: "rag.retrieve", dur: "0.8s", dot: "var(--green)", indent: "12px", tokens: "" },
  { name: "tool.terraform_plan", dur: "3.4s", dot: "var(--green)", indent: "24px", tokens: "" },
  { name: "policy.evaluate", dur: "0.4s", dot: "var(--green)", indent: "12px", tokens: "" },
  { name: "approval.gate", dur: "···", dot: "var(--amber)", indent: "0px", tokens: "" },
];

export const logLines = [
  { ts: "14:23:41", lvl: "INFO", lvlColor: "var(--cyan)", msg: "intent classified: provisioning (0.98)" },
  { ts: "14:23:41", lvl: "INFO", lvlColor: "var(--cyan)", msg: "routed -> CloudOps agent" },
  { ts: "14:23:42", lvl: "INFO", lvlColor: "var(--cyan)", msg: "discovered vpc-0a91c4f2 · 6 subnets" },
  { ts: "14:23:43", lvl: "DEBUG", lvlColor: "var(--text-4)", msg: "rag: 2 runbooks · 1 design doc" },
  { ts: "14:23:44", lvl: "INFO", lvlColor: "var(--cyan)", msg: "terraform init · backend s3" },
  { ts: "14:23:47", lvl: "INFO", lvlColor: "var(--cyan)", msg: "plan: +14 ~2 -0" },
  { ts: "14:23:47", lvl: "OK", lvlColor: "var(--green)", msg: "policy: 6/6 passed" },
  { ts: "14:23:48", lvl: "WARN", lvlColor: "var(--amber)", msg: "awaiting human approval" },
];

export const reasoningCards = [
  { title: "Interpreted intent", conf: "98%", body: "Provision EKS · reuse existing prod VPC · 3 AZ · managed node groups. No destructive operation detected." },
  { title: "Routing decision", conf: "", body: "Routed to CloudOps agent over DevOps/SRE — request is infrastructure provisioning, not deployment or incident." },
  { title: "Workflow selection", conf: "", body: "Selected eks-provision v3 with terraform-aws-eks v20.8 (org-approved). Rejected raw HCL — violates module policy." },
  { title: "Risk evaluation", conf: "", body: "Production + new IAM + $312/mo → medium risk. prod-change-control requires human approval before apply." },
  { title: "Memory applied", conf: "", body: "Reused naming convention and the $500/mo cost guardrail from prior context." },
];

export interface WorkflowNode {
  title: string;
  detail: string;
  time: string;
  bg: string;
  ring: string;
  titleColor: string;
  lineColor: string;
  status: string;
  hasLine: boolean;
}

export function workflowNodes(approval: ApprovalState): WorkflowNode[] {
  const approved = approval === "approved";
  const rejected = approval === "rejected";
  const defs = [
    { title: "Router", detail: "Classified → provisioning intent", time: "0.3s", status: "done" },
    { title: "Planner", detail: "Decomposed into 4 sub-tasks", time: "0.6s", status: "done" },
    { title: "CloudOps Agent", detail: "Discovered VPC, drafted plan", time: "3.4s", status: "done" },
    { title: "Policy Evaluation", detail: "6 of 6 checks passed", time: "0.4s", status: "done" },
    {
      title: "Human Approval",
      detail: rejected ? "Rejected — halted" : approved ? "Approved by you" : "Awaiting your decision",
      time: approved ? "—" : rejected ? "—" : "···",
      status: rejected ? "rejected" : approved ? "done" : "pending",
    },
    {
      title: "Terraform Apply",
      detail: approved ? "Applying 14 resources…" : "Queued",
      time: approved ? "···" : "—",
      status: approved ? "running" : rejected ? "cancelled" : "queued",
    },
    { title: "Verification", detail: "Pending apply", time: "—", status: rejected ? "cancelled" : "queued", last: true },
  ];
  const palette: Record<string, { bg: string; ring: string; titleColor: string; line: string }> = {
    done: { bg: "rgba(52,211,153,.13)", ring: "var(--green)", titleColor: "var(--text)", line: "rgba(52,211,153,.4)" },
    running: { bg: "rgba(129,140,248,.16)", ring: "var(--accent-2)", titleColor: "var(--text)", line: "rgba(129,140,248,.4)" },
    pending: { bg: "rgba(251,191,36,.16)", ring: "var(--amber)", titleColor: "var(--text)", line: "var(--border-2)" },
    queued: { bg: "var(--surface-3)", ring: "var(--border-3)", titleColor: "var(--text-3)", line: "var(--border-2)" },
    rejected: { bg: "rgba(248,113,113,.16)", ring: "var(--red)", titleColor: "var(--text)", line: "rgba(248,113,113,.3)" },
    cancelled: { bg: "var(--surface-2)", ring: "var(--border-2)", titleColor: "var(--text-5)", line: "var(--border)" },
  };
  return defs.map((d) => {
    const p = palette[d.status];
    return {
      title: d.title,
      detail: d.detail,
      time: d.time,
      bg: p.bg,
      ring: p.ring,
      titleColor: p.titleColor,
      lineColor: p.line,
      status: d.status,
      hasLine: !d.last,
    };
  });
}

export const artifactTitles: Record<string, string> = {
  timeline: "Workflow Timeline",
  reasoning: "Agent Reasoning",
  terraform: "Terraform Plan",
  logs: "Execution Logs",
  metrics: "Metrics",
  traces: "Langfuse Traces",
  references: "References",
  approvals: "Approvals",
};

export const artifactTabList: [string, string][] = [
  ["timeline", "Timeline"],
  ["reasoning", "Reasoning"],
  ["terraform", "Terraform"],
  ["logs", "Logs"],
  ["metrics", "Metrics"],
  ["traces", "Traces"],
  ["references", "References"],
  ["approvals", "Approvals"],
];
