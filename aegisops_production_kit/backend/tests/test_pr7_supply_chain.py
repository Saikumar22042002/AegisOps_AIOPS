"""PR-7 SUPPLY — CI runs pip-audit (backend) + npm audit (frontend); the API image base is
pinned by digest with a documented rebuild cadence. Structural checks (the audits themselves
run in CI, not here)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def _find(rel: str) -> Path:
    from app import metrics
    backend = Path(metrics.__file__).resolve().parents[1]   # backend/ (host) or /app (container)
    for base in (backend, backend.parent):
        p = base / rel
        if p.exists():
            return p
    raise FileNotFoundError(rel)


def test_ci_has_the_supply_chain_job():
    ci = yaml.safe_load(_find(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert "supply-chain" in jobs
    steps = jobs["supply-chain"]["steps"]
    blob = yaml.safe_dump(steps)
    assert "pip-audit" in blob
    assert "npm audit" in blob and "--audit-level=high" in blob


def test_api_base_image_is_digest_pinned():
    df = _find("Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^FROM python:3\.11-slim@sha256:([0-9a-f]{64}) AS base", df, re.MULTILINE)
    assert m, "the API base image must be pinned by @sha256 digest (PR-7)"
    assert "rebuild cadence" in df.lower()      # cadence documented at the pin


def test_scanners_still_pinned_from_the_scan_commit():
    """PR-7 image hygiene keeps the SCAN commit's pinned scanner versions."""
    df = _find("Dockerfile").read_text(encoding="utf-8")
    assert "checkov==3.3.8" in df and "TFSEC_VERSION=v1.28.14" in df
