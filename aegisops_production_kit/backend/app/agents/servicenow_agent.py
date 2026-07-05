"""ServiceNow sub-agent — update/close the run's SR/CR/Incident with the outcome + artifacts."""

from __future__ import annotations

import structlog

from ..integrations.servicenow import get_servicenow
from ..settings import get_settings
from .state import AgentState

log = structlog.get_logger(__name__)


async def servicenow_update(state: AgentState, config) -> dict:
    snow = get_servicenow(get_settings())
    sys_id = state.get("snow_sys_id")
    table = state.get("snow_table")
    if not snow.enabled or not sys_id or not table:
        return {}
    status = state.get("approval_status")
    outcome = state.get("outcome", {})
    try:
        note = f"AegisOps run {state['run_id']}: {state.get('resolution', '')}"
        await snow.add_work_note(table, sys_id, note)
        out_status = str(outcome.get("status") or "")
        if status == "rejected":
            await snow.update(table, sys_id, {"work_notes": "Change rejected by approver; no action taken."})
        elif outcome.get("status") in {"applied", "destroyed"}:
            await snow.close(table, sys_id, close_notes=state.get("resolution", "Resolved by AegisOps"))
        elif out_status.endswith("_failed") or out_status == "failed":
            # Resolve the record as FAILED with the classified cause + next step (Phase 7 / BUG-05).
            f = outcome.get("failure") if isinstance(outcome.get("failure"), dict) else {}
            detail = (f or {}).get("title") or outcome.get("error", "provider error")
            next_step = (f or {}).get("next_step", "")
            await snow.update(table, sys_id, {"work_notes":
                              f"Run FAILED: {str(detail)[:300]}." + (f" Next step: {next_step}" if next_step else "")})
            await snow.close(table, sys_id, close_notes=f"Failed — {str(detail)[:200]}")
        log.info("servicenow.updated", table=table, sys_id=sys_id, status=status)
    except Exception as e:  # noqa: BLE001
        log.warning("servicenow.update_failed", error=str(e))
    return {}
