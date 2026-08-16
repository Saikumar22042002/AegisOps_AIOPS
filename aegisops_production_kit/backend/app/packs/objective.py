"""Objective model — provider-neutral capability resolution (P4.1 — Redesign/03 §1, 00 §5).

The senior-engineer behavior: a user says "create a VM" / "find my VPC" / "open port 8501"
and the system resolves the capability FAMILY (compute / network / firewall) and the target
provider (aws|azure|gcp) from intent + context — never requiring the user to name EC2 vs
Azure VM vs GCE. This is a DETERMINISTIC pre-classifier that feeds the harness; the harness
does the reasoning over the resolved pack tools. It contains no provider-specific control
flow — only the capability-family vocabulary and a provider hint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..settings import Settings
from . import registry
from .base import CAPABILITY_FAMILIES

# Intent phrase → capability family (provider-neutral). The user's words map to a family;
# the pack owns the provider-specific implementation of that family.
_FAMILY_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(vms?|instances?|compute|servers?|ec2|virtual machines?|gce)\b", re.I), "compute"),
    (re.compile(r"\b(vpcs?|vnets?|networks?|subnets?|firewalls?|security groups?|nsg|ports?)\b", re.I), "network"),
    (re.compile(r"\b(buckets?|blob|object storage|s3|gcs|storage accounts?)\b", re.I), "storage"),
    (re.compile(r"\b(databases?|rds|sql|postgres|cloudsql|db)\b", re.I), "db"),
    (re.compile(r"\b(kubernetes|k8s|eks|aks|gke|clusters?|pods?|deployments?)\b", re.I), "k8s"),
    (re.compile(r"\b(metrics?|monitor|cloudwatch|telemetry|5xx|latency|error rate)\b", re.I), "telemetry"),
    (re.compile(r"\b(repos?|repositor(?:y|ies)|pull requests?|\bpr\b|commits?|branch(?:es)?)\b", re.I), "repo"),
    (re.compile(r"\b(workflows?|actions|ci|pipelines?|builds?)\b", re.I), "ci"),
    (re.compile(r"\b(load balancers?|elb|alb|nlb|app gateway)\b", re.I), "lb"),
]

# Provider hints use only provider-UNIQUE terms — "vpc"/"network"/"vm" are shared vocabulary
# across clouds and must NOT force a provider (the objective stays provider-neutral until the
# user names one or context resolves it).
_PROVIDER_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(aws|amazon|ec2|eks|s3|rds)\b", re.I), "aws"),
    (re.compile(r"\b(azure|aks|vnets?|blob|entra)\b", re.I), "azure"),
    (re.compile(r"\b(gcp|google cloud|gce|gke|gcs|cloudsql)\b", re.I), "gcp"),
    (re.compile(r"\b(github|actions|pull requests?|repos?)\b", re.I), "github"),
    (re.compile(r"\b(kubernetes|k8s|pods?|kubectl)\b", re.I), "k8s"),
]


@dataclass(frozen=True)
class Objective:
    text: str
    family: str | None            # a CAPABILITY_FAMILIES member, or None if unresolved
    provider: str | None          # aws|azure|gcp|github|k8s, or None → ask/infer
    read_only: bool               # a question ⇒ read-only investigation


def _is_question(text: str) -> bool:
    t = text.strip().lower()
    return (t.endswith("?") or t.startswith((
        "how many", "what", "which", "are there", "is there", "do i", "did i",
        "list", "show", "find", "investigate", "why", "check", "diagnose")))


def classify(text: str) -> Objective:
    family = next((fam for rx, fam in _FAMILY_HINTS if rx.search(text)), None)
    provider = next((p for rx, p in _PROVIDER_HINTS if rx.search(text)), None)
    return Objective(text=text, family=family, provider=provider,
                     read_only=_is_question(text))


def resolve_tools(settings: Settings, objective: Objective) -> list[str]:
    """The provider-neutral tool resolution: which configured pack read tools serve this
    objective's family (across all providers when none is named — the harness then reasons
    over them). Empty when nothing matches — the harness asks rather than guessing."""
    tools: list[str] = []
    for pack in registry.configured_packs(settings):
        if objective.provider and pack.provider != objective.provider:
            continue
        for tool in pack.read_tools():
            if objective.family is None or tool.family == objective.family:
                tools.append(tool.name)
    return sorted(tools)
