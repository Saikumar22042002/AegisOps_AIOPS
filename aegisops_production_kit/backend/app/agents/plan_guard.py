"""Action-vs-operation hard guard (Phase 8 / N-08) — the last line of defense against the
destructive class.

Whatever the classifier said and whatever state Terraform reconciled against, the plan that is
about to be shown to an approver (and later applied) must MATCH the user's action:

  create  → may only add (no delete, no replace): a benign create must never tear anything down;
  modify  → updates in place only (a replace silently destroys the instance — surface, don't run);
  destroy → may only delete (a destroy must never provision);
  read    → must never reach a Terraform plan at all.

`check_plan_actions` is pure (plan diff in, violation message out) so it is trivially testable
and cannot be bypassed by prompt or state weirdness. CloudOps calls it after `show_plan()` and
BEFORE the approval interrupt; a violation halts the run with an explanation.
"""

from __future__ import annotations

_MUTATING = ("create", "delete", "update")


def _classify_rc(actions: list[str]) -> str:
    """One resource-change entry → create | delete | replace | update | noop."""
    acts = set(a.lower() for a in (actions or []))
    if {"create", "delete"} <= acts:
        return "replace"
    if "delete" in acts:
        return "delete"
    if "create" in acts:
        return "create"
    if "update" in acts:
        return "update"
    return "noop"


def check_plan_actions(action: str, diff: list[dict]) -> str | None:
    """Return None when the plan is consistent with the classified action, else a
    human-readable violation that must halt the run before the approval gate."""
    action = (action or "").lower()
    kinds = [( _classify_rc(rc.get("actions", [])), rc.get("address") or "?") for rc in (diff or [])]
    deletes = [addr for k, addr in kinds if k == "delete"]
    replaces = [addr for k, addr in kinds if k == "replace"]
    creates = [addr for k, addr in kinds if k in ("create", "replace")]
    updates = [addr for k, addr in kinds if k == "update"]

    if action == "read":
        if any(k != "noop" for k, _ in kinds):
            return ("Safety guard: this was a read-only request, but a Terraform plan with "
                    "changes was produced. Read operations never run Terraform — halting.")
        return None

    if action == "create":
        if deletes or replaces:
            victims = ", ".join((deletes + replaces)[:5])
            return ("Safety guard: you asked to CREATE, but this plan would destroy or replace "
                    f"existing infrastructure ({victims}). A create must never tear anything "
                    "down — this usually means the workspace state was shared with a previous "
                    "resource. Halting; nothing was changed.")
        return None

    if action == "modify":
        if deletes or replaces:
            victims = ", ".join((deletes + replaces)[:5])
            return ("Safety guard: this modification would destroy or replace "
                    f"{victims} rather than update it in place. That is effectively a "
                    "destroy — halting so you can decide explicitly.")
        return None

    if action == "destroy":
        if creates or updates:
            extras = ", ".join((creates + updates)[:5])
            return ("Safety guard: you asked to DESTROY, but this plan would also create or "
                    f"modify resources ({extras}). A destroy must only remove the confirmed "
                    "target — halting; nothing was changed.")
        return None

    return f"Safety guard: unknown action '{action}' cannot be verified against the plan — halting."
