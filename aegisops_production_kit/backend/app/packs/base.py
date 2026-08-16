"""Capability-pack contract (P4 — Redesign/02 §4, 05 §10).

A pack exports the 02 §4 fields as reviewable data + tool registrations:
`name`, `tools`, `knowledge`, `playbooks`, `verify`, `templates`, `day2`, `policies`.
P4 ships the READ tool surface + declared mutation metadata; the harness reasons over the
read tools, and mutation stays the governed exec_loop/approval/P3 path.

`ToolSpec.effect` is `read | propose | mutation-ref` (05 §2). Only `read` tools enter the
harness INV registry — the read-only-by-construction guarantee is preserved (a mutation
name still trips the investigation registry's denylist at registration). `mutation` and
`propose` specs are metadata the objective model + engine use; they are never executed as
read tools.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Effect = Literal["read", "propose", "mutation"]

# Provider-neutral capability families (03 §3 normalized resource model). The objective
# model maps a user intent → a family → the resolved provider's tool. This is the
# cloud-neutral vocabulary the harness reasons in ("I need compute", never "I need EC2").
CAPABILITY_FAMILIES: frozenset[str] = frozenset({
    "compute", "network", "storage", "db", "k8s", "serverless", "identity",
    "telemetry", "lb", "repo", "ci",
})


@dataclass(frozen=True)
class ToolSpec:
    """One capability tool. `fn` is present only for read tools (executed by the harness);
    mutation/propose specs carry metadata (template_key/day2_verb) instead."""
    name: str                       # namespaced, e.g. "cloudops.aws.list_vpcs"
    description: str
    family: str                     # a CAPABILITY_FAMILIES member
    effect: Effect
    fn: Callable[..., Awaitable[Any]] | None = None
    template_key: str | None = None   # for mutation specs (approved Terraform catalog)
    day2_verb: str | None = None      # for lifecycle (day-2) specs


@dataclass(frozen=True)
class CapabilityPack:
    """A thin domain specialist (02 §4). Cloud/provider identity lives in `name` +
    per-tool implementations; nothing above the pack is provider-specific."""
    name: str                       # "cloudops.aws" | "cloudops.azure" | … | "sreops.k8s"
    provider: str                   # aws | azure | gcp | github | k8s
    domain: str                     # cloudops | devops | sreops
    tools: tuple[ToolSpec, ...] = ()
    knowledge: tuple[str, ...] = ()
    playbooks: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()   # approved Terraform template keys this pack owns
    day2: tuple[str, ...] = ()        # day-2 verb keys this pack owns
    enabled: Callable[[Any], bool] = lambda settings: True   # credentials present?

    def read_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(t for t in self.tools if t.effect == "read" and t.fn is not None)

    def mutation_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t for t in self.tools if t.effect in ("mutation", "propose"))
