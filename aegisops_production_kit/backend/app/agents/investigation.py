"""Read-only investigation agents (INV).

Locked decision 13: sub-agent work is allowed ONLY for read-only investigation (SRE triage,
discovery); **mutation is never delegated to a spawned agent**. This module is that boundary,
made structural rather than aspirational:

* a **registry of read-only tools** — registration ASSERTS the tool is read-only (mutation-
  marker names are rejected outright), and the registry is frozen once built;
* an **Investigator** that can call only registered tools, under a hard call budget, and
  records every call + result as evidence (fed to the context graph / analysis);
* sub-investigations share the SAME frozen registry — a spawned investigator cannot acquire
  tools its parent didn't have, so delegation can never widen into mutation.

The deepagents package (decision gate: read-only investigation only, re-evaluate at 1.0/LTS)
would plug in as a director that chooses which registered tools to call; today's director is
deterministic. Either way the tool surface — and therefore the blast radius — is this registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

MAX_CALLS = 8  # hard budget per investigation — bounded, never open-ended

# A tool whose name carries any of these is a mutation and can never be registered. This is a
# denylist on top of an allowlist habit (get_/list_/describe_/query_/read_/ping) — belt and
# suspenders, checked at REGISTRATION so a bad tool never becomes callable.
_MUTATION_MARKERS = ("apply", "create", "delete", "destroy", "patch", "scale", "restart",
                     "rollback", "write", "set_", "update", "push", "dispatch", "terminate",
                     "upsert", "remove", "revoke", "put_")


class ReadOnlyViolation(Exception):
    """Raised when something tries to smuggle a mutating tool into an investigation."""


@dataclass(frozen=True)
class ReadOnlyTool:
    name: str
    description: str
    fn: Callable[..., Awaitable[Any]]


def assert_read_only(name: str) -> None:
    lowered = name.lower()
    for marker in _MUTATION_MARKERS:
        if marker in lowered:
            raise ReadOnlyViolation(
                f"tool '{name}' looks like a mutation ('{marker}') — investigation agents are "
                "read-only; mutation is never delegated to a spawned agent")


class ToolRegistry:
    """Frozen-after-build registry: the ONLY tool surface an investigator can reach."""

    def __init__(self) -> None:
        self._tools: dict[str, ReadOnlyTool] = {}
        self._frozen = False

    def register(self, name: str, description: str, fn: Callable[..., Awaitable[Any]]) -> None:
        if self._frozen:
            raise ReadOnlyViolation("registry is frozen — a running investigation cannot grow "
                                    "its tool surface")
        assert_read_only(name)
        self._tools[name] = ReadOnlyTool(name=name, description=description, fn=fn)

    def freeze(self) -> "ToolRegistry":
        self._frozen = True
        return self

    def get(self, name: str) -> ReadOnlyTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)


@dataclass
class Evidence:
    tool: str
    args: dict
    ok: bool
    result: Any = None
    error: str = ""


@dataclass
class Investigator:
    """Runs a bounded sequence of read-only tool calls and returns the evidence trail.

    `spawn()` creates a sub-investigator over the SAME frozen registry sharing the SAME call
    budget — sub-agents can go no wider and no deeper than their parent's allowance.
    """

    registry: ToolRegistry
    max_calls: int = MAX_CALLS
    _calls_used: list[int] = field(default_factory=lambda: [0])  # shared across spawns

    async def call(self, tool_name: str, **args: Any) -> Evidence:
        if self._calls_used[0] >= self.max_calls:
            return Evidence(tool=tool_name, args=args, ok=False,
                            error=f"investigation budget exhausted ({self.max_calls} calls)")
        tool = self.registry.get(tool_name)
        if tool is None:
            return Evidence(tool=tool_name, args=args, ok=False,
                            error=f"'{tool_name}' is not a registered read-only tool — refused")
        self._calls_used[0] += 1
        try:
            result = await tool.fn(**args)
            return Evidence(tool=tool_name, args=args, ok=True, result=result)
        except Exception as e:  # noqa: BLE001 — a failed read is evidence, not a crash
            log.warning("investigation.tool_failed", tool=tool_name, error=str(e))
            return Evidence(tool=tool_name, args=args, ok=False, error=str(e)[:300])

    async def run(self, plan: list[dict]) -> list[Evidence]:
        """Execute an ordered investigation plan: [{"tool": name, "args": {...}}, …]."""
        return [await self.call(step.get("tool", ""), **(step.get("args") or {}))
                for step in plan]

    def spawn(self) -> "Investigator":
        """A sub-investigator: same frozen registry, same shared budget — never wider."""
        return Investigator(registry=self.registry, max_calls=self.max_calls,
                            _calls_used=self._calls_used)


def default_registry(settings) -> ToolRegistry:
    """The real read-only tool surface: Prometheus, K8s reads, inventory, world model.

    Every entry is a discovery/verification read (the hard rule: SDKs never provision).
    """
    from ..graph_db import world_model
    from ..tools.kubernetes import get_kubernetes
    from ..tools.prometheus import get_prometheus
    from . import inventory

    reg = ToolRegistry()
    prom = get_prometheus(settings)
    k8s = get_kubernetes(settings)

    async def prom_query(query: str, default: float = 0.0) -> float:
        return await prom.scalar(query, default=default)

    reg.register("query_prometheus", "PromQL scalar query", prom_query)
    reg.register("list_deployments", "K8s deployments in a namespace",
                 k8s.list_deployments)
    reg.register("list_pods", "K8s pods in a namespace", k8s.list_pods)
    reg.register("list_inventory", "Active provisioned resources for the org",
                 inventory.list_active)
    reg.register("query_impact", "World-model dependents of a resource",
                 world_model.impact_of)
    return reg.freeze()
