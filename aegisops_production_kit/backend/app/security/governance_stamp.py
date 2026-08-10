"""P0 governance stamping (Redesign/04 §8.4, 07 item 0.5, defect D9/F-9).

The defect this closes: governance-relevant flags can drift via `.env` with zero
visibility to the humans approving changes. The fix is visibility, not new policy:
a snapshot of the active governance posture is attached to every approval interrupt
payload (web + gateway cards render it), exposed on `/healthz`, and therefore lands
in the durable approval/audit trail alongside the decision.

This module is deliberately a pure read of `Settings` — it introduces no new
permission architecture and changes no enforcement behavior (P0 boundary).
"""

from __future__ import annotations

from typing import Any

from ..settings import Settings, get_settings


def governance_stamp(settings: Settings | None = None) -> dict[str, Any]:
    """A JSON-safe snapshot of the governance flags in force for this process."""
    s = settings or get_settings()
    return {
        "app_env": s.app_env,
        "role": getattr(s, "aegisops_role", "all"),
        "tenancy": s.aegisops_tenancy,
        "event_bus": s.aegisops_event_bus,
        "exec_loop": s.aegisops_exec_loop,
        "drift": s.aegisops_drift,
        "default_execution_mode": s.default_execution_mode,
        # Single-user human-in-the-loop is THE approval model (Redesign/00 §7): the
        # initiating human reviews and approves their own plan (initiator == approver).
        # There is no second-approver / four-eyes concept in AegisOps.
        "approval_model": "hitl",
    }


def stamped(payload: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Return a copy of an approval interrupt payload carrying the governance snapshot.

    Additive only: existing payload keys are never touched, so the approval card,
    resume flow, and every existing consumer see exactly the fields they saw before,
    plus `governance`.
    """
    return {**payload, "governance": governance_stamp(settings)}
