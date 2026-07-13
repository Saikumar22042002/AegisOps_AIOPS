"""Prometheus metrics registry for AegisOps.

Exposed at GET /metrics. Metric names match the provisioned Grafana dashboard
(`infra/grafana/dashboards/aegisops-overview.json`).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# A dedicated registry keeps app metrics isolated and testable.
REGISTRY = CollectorRegistry()

API_REQUESTS = Counter(
    "aegisops_api_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

API_REQUEST_DURATION = Histogram(
    "aegisops_api_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

AGENT_RUNS = Counter(
    "aegisops_agent_runs_total",
    "Agent runs by domain and final status.",
    labelnames=("domain", "workflow", "status", "env"),
    registry=REGISTRY,
)

AGENT_STEP_DURATION = Histogram(
    "aegisops_agent_step_duration_seconds",
    "Per-step duration of agent graph execution.",
    labelnames=("agent", "step"),
    registry=REGISTRY,
)

LLM_LATENCY = Histogram(
    "aegisops_llm_latency_seconds",
    "Gemini call latency in seconds.",
    labelnames=("model", "operation"),
    buckets=(0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 20.0),
    registry=REGISTRY,
)

RAG_LATENCY = Histogram(
    "aegisops_rag_latency_seconds",
    "RAG retrieval latency in seconds.",
    labelnames=("operation",),
    registry=REGISTRY,
)

APPROVAL_WAIT = Histogram(
    "aegisops_approval_wait_seconds",
    "Wall-clock time a run waited at the human-approval gate.",
    labelnames=("domain", "decision"),
    buckets=(1, 5, 15, 30, 60, 300, 900, 3600),
    registry=REGISTRY,
)

DEP_UP = Gauge(
    "aegisops_dependency_up",
    "Liveness of a backing dependency (1 up, 0 down).",
    labelnames=("dependency",),
    registry=REGISTRY,
)

# ── PR-6: operator-alert signals, set by the reconciler each sweep ──
STRANDED_RUNS = Gauge(
    "aegisops_stranded_runs",
    "Runs recovered/marked-failed by the last reconcile sweep (stranded after a crash).",
    registry=REGISTRY,
)

RECONCILER_SWEEP_FAILURES = Counter(
    "aegisops_reconciler_sweep_failures_total",
    "Reconcile sweeps that raised (the loop caught + logged; count trends operator attention).",
    registry=REGISTRY,
)

DRIFT_FINDINGS = Gauge(
    "aegisops_drift_findings",
    "Drift/orphan findings from the last reader-seam sweep.",
    labelnames=("kind",),  # drift | orphan
    registry=REGISTRY,
)
