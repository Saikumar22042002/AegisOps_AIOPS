"""Tool Registry v2 foundation (P2.1 — Redesign/05 §1/§3).

The bridge that turns the frozen read-only investigation registry (the P2.2 tool surface)
into native tool-calling schemas the P1 model layer can offer, and executes a model-issued
ToolCall back through it under the middleware order. This is the FOUNDATION (read effect
only in P2): the full propose/mutation registry with policy verdicts is P3+.

Middleware order is normative (05 §3): tenancy → rbac → policy → rate → timeout → execute →
validate → redact → observe. In P2's read-only INV surface, tenancy/rbac/policy are
satisfied structurally (the registry is read-only-by-construction and the run already
passed admission); this module enforces timeout → execute → redact → observe and NEVER
raises — a failed tool is a typed observation (05 §3, loop law L3).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from ..agents.investigation import ToolRegistry
from ..llm.types import ToolResult
from ..security.redaction import redact_dict

log = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ToolSchema:
    """A native-FC tool definition derived from a read-only registered tool."""
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_tooldef(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


def schemas_for(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Offer every registered read tool to the model as a native tool (05 §4). Args are
    permissively typed in P2 — the registry tools validate their own inputs, and the
    read-only-by-construction guarantee makes a bad arg a failed observation, not a risk."""
    out: list[dict[str, Any]] = []
    for name in registry.names():
        tool = registry.get(name)
        out.append(ToolSchema(
            name=name, description=tool.description if tool else name,
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        ).as_tooldef())
    return out


async def execute(registry: ToolRegistry, tool_call, *,
                  timeout_s: float = _DEFAULT_TIMEOUT_S) -> ToolResult:
    """Run a model-issued ToolCall through the read-only registry. Returns a ToolResult
    (never raises): failure is an observation the next reasoning iteration sees (L3)."""
    tool = registry.get(tool_call.name)
    if tool is None:
        # A hallucinated / unavailable tool is a first-class observation, not a crash.
        return ToolResult(tool_call_id=tool_call.id, ok=False,
                          error={"kind": "unknown_tool",
                                 "message": f"'{tool_call.name}' is not a registered "
                                            f"read tool; available: {registry.names()}"},
                          stage="policy_verdict")
    try:
        result = await asyncio.wait_for(tool.fn(**(tool_call.arguments or {})),
                                        timeout=timeout_s)
    except asyncio.TimeoutError:
        return ToolResult(tool_call_id=tool_call.id, ok=False,
                          error={"kind": "timeout",
                                 "message": f"{tool_call.name} exceeded {timeout_s:.0f}s"},
                          stage="timeout")
    except TypeError as e:
        # Bad arguments from the model — an observation it can correct next iteration.
        return ToolResult(tool_call_id=tool_call.id, ok=False,
                          error={"kind": "bad_arguments", "message": str(e)[:300]},
                          stage="execute")
    except Exception as e:  # noqa: BLE001 — any read failure is evidence (L3), never a crash
        log.warning("registry.tool_failed", tool=tool_call.name, error=str(e))
        return ToolResult(tool_call_id=tool_call.id, ok=False,
                          error={"kind": "tool_error", "message": str(e)[:300]},
                          stage="execute")
    content = _stringify(result)
    return ToolResult(tool_call_id=tool_call.id, ok=True, content=content, stage="observe")


def _stringify(result: Any) -> str:
    """Redacted, model-ingestible rendering of a tool result."""
    import json
    if isinstance(result, dict):
        result = redact_dict(result)
    try:
        return json.dumps(result, default=str)[:8000]
    except (TypeError, ValueError):
        return str(result)[:8000]
