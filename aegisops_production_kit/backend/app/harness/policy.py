"""Permission modes + ESTOP (P4.5 — Redesign/03 §6.1, 04 §8.1/§8.6).

The policy matrix over the four modes — READ_ONLY, PLAN_ONLY, APPROVAL_REQUIRED (default
for new orgs), AUTONOMOUS — deciding, per action effect and risk, whether it may run, needs
a plan, needs approval, or is denied. AUTONOMOUS is NEVER enabled by this build (P4 prompt +
09 do-not-start list): it is present in the matrix as the frozen target, but `evaluate`
refuses to grant it unless an explicit, separately-gated allowlist is passed — which this
phase does not wire. ESTOP is a platform sentinel that pauses new mutations while letting
in-flight applies finish.

This is deterministic policy, not reasoning. It contains no provider-specific logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import structlog

log = structlog.get_logger(__name__)


class Mode(str, Enum):
    READ_ONLY = "READ_ONLY"
    PLAN_ONLY = "PLAN_ONLY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"   # default for new orgs (03 §6.1)
    AUTONOMOUS = "AUTONOMOUS"                  # never enabled in this build


Effect = Literal["read", "propose", "mutation"]
Risk = Literal["read", "low", "medium", "high", "destructive"]
Decision = Literal["allow", "plan_only", "approval_required", "deny"]

# ESTOP sentinel — process/platform-level. When engaged, no NEW mutation may start;
# in-flight applies finish (04 §8.6). A tiny module-global is the interim carrier; a durable
# store is P5.
_ESTOP = {"engaged": False, "reason": ""}


def engage_estop(reason: str) -> None:
    _ESTOP["engaged"] = True
    _ESTOP["reason"] = reason[:200]
    log.warning("policy.estop_engaged", reason=reason)


def clear_estop() -> None:
    _ESTOP["engaged"] = False
    _ESTOP["reason"] = ""


def estop_engaged() -> bool:
    return bool(_ESTOP["engaged"])


@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    reason: str


def evaluate(*, mode: Mode, effect: Effect, risk: Risk = "read",
             autonomous_allowlisted: bool = False) -> PolicyVerdict:
    """Deterministic permission decision. Reads flow freely; mutations route by mode/risk.

    AUTONOMOUS grants a mutation WITHOUT approval only for a caller that is BOTH in
    AUTONOMOUS mode AND on a separately-gated allowlist AND below the destructive ceiling —
    a combination this phase never assembles, so autonomous mutation cannot occur here."""
    if effect == "read":
        return PolicyVerdict("allow", "read effect flows freely")

    # ESTOP: no new mutation starts while engaged (in-flight applies are elsewhere).
    if estop_engaged():
        return PolicyVerdict("deny", f"ESTOP engaged: {_ESTOP['reason']}")

    if mode == Mode.READ_ONLY:
        return PolicyVerdict("deny", "READ_ONLY mode forbids mutation")
    if mode == Mode.PLAN_ONLY:
        return PolicyVerdict("plan_only", "PLAN_ONLY mode: compile a plan, do not execute")

    # destructive is ALWAYS human-gated regardless of mode (03 §3.4 rule 6, 04 §8).
    if risk == "destructive":
        return PolicyVerdict("approval_required", "destructive risk is always human-gated")

    if mode == Mode.AUTONOMOUS:
        if autonomous_allowlisted and risk in ("read", "low"):
            return PolicyVerdict("allow", "AUTONOMOUS + allowlisted + low risk")
        return PolicyVerdict("approval_required",
                             "AUTONOMOUS not allowlisted for this risk — falls back to approval")

    # APPROVAL_REQUIRED (default) and any propose/mutation: human approves.
    return PolicyVerdict("approval_required", "mutation requires human approval (HITL)")
