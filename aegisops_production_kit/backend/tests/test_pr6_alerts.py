"""PR-6 ALERTS — Prometheus rules ship, parse clean (promtool when available, else a
structural check), reference metrics AegisOps actually exposes, and each rule carries a
runbook line. The new operator gauges exist and the reconciler publishes them."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def _find(rel: str) -> Path:
    from app import metrics
    backend = Path(metrics.__file__).resolve().parents[1]   # backend/ (host) or /app (container)
    for base in (backend, backend.parent):
        p = base / rel
        if p.exists():
            return p
    raise FileNotFoundError(rel)


def _alerts_path() -> Path:
    return _find("infra/prometheus/alerts.yml")


def test_alerts_file_parses_and_every_rule_has_a_runbook():
    doc = yaml.safe_load(_alerts_path().read_text(encoding="utf-8"))
    groups = doc["groups"]
    rules = [r for g in groups for r in g["rules"]]
    assert len(rules) >= 6
    for r in rules:
        assert r.get("alert") and r.get("expr")
        assert r.get("annotations", {}).get("runbook"), f"{r.get('alert')} missing a runbook line"


def test_promtool_lints_clean_if_available():
    if not shutil.which("promtool"):
        pytest.skip("promtool not installed in this environment")
    res = subprocess.run(["promtool", "check", "rules", str(_alerts_path())],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_rules_reference_real_exposed_metrics():
    from app import metrics
    exposed = {m.name for m in metrics.REGISTRY.collect()}
    # the metric families the rules lean on must exist in the registry
    for needed in ("aegisops_stranded_runs", "aegisops_reconciler_sweep_failures",
                   "aegisops_drift_findings", "aegisops_api_requests",
                   "aegisops_agent_runs", "aegisops_dependency_up"):
        assert any(name.startswith(needed) for name in exposed), needed


def test_new_gauges_and_reconciler_wiring():
    from app import metrics
    assert hasattr(metrics, "STRANDED_RUNS")
    assert hasattr(metrics, "RECONCILER_SWEEP_FAILURES")
    assert hasattr(metrics, "DRIFT_FINDINGS")
    # the reconciler publishes them (pin at the source so a refactor can't drop it)
    src = _find("app/agents/reconciler.py").read_text(encoding="utf-8")
    assert "STRANDED_RUNS.set" in src and "RECONCILER_SWEEP_FAILURES.inc" in src


def test_prometheus_loads_the_rule_file():
    cfg = yaml.safe_load(
        _find("infra/prometheus/prometheus.yml").read_text(encoding="utf-8"))
    assert "alerts.yml" in (cfg.get("rule_files") or [])
