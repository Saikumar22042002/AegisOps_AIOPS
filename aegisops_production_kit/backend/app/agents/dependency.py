"""Dependency closure resolution (DEP).

A provisioning request whose module needs a parent resource (EC2 → VPC/subnet, EKS → VPC +
subnets, Azure storage/VM/AKS → resource group) resolves that parent in a STRICT order —
locked decision:

1. **named** — the user named it (the validated inputs already carry a value) → use it.
2. **world model** — existing suitable resources in the org's inventory: exactly one → use it
   (stated on the card); two or more → ASK, offering them (never guess a placement).
3. **stated default** — the module's declared default placement (EC2 → the default VPC's
   subnet; Azure VM/AKS → a module-created `<name>-rg`) → proceed, stating the default.
4. **create-first DAG** — nothing suitable and no default (or the user explicitly asked for a
   NEW parent) → an ordered goal DAG that creates the parent first and wires its outputs into
   the child. The DAG is data — the Governed Executive Loop (U6) executes it; nothing here
   mutates anything.

Candidate discovery reads the DB inventory (the world model's tabular source of truth —
org-scoped, always available); dependency edges/impact live in the Neo4j world model (D3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Slot:
    """One dependency a template's inputs carry."""

    field: str                      # the child input that names the parent
    parent_cloud: str
    parent_type: str                # inventory resource_type of the parent
    required: bool                  # schema-required (no module default)
    creator: str                    # template key that creates the parent (create-first)
    wires: dict[str, str]           # child_field -> parent output expr (or "input:<name>")
    stated_default: str | None = None   # module's declared default placement, when one exists
    value_from: str = "provider_id"
    # value_from: where the slot's value comes from on an EXISTING parent —
    #   "provider_id" (EKS vpc_id ← vpc-…), "name" (Azure RGs are addressed by name), or
    #   "attr:<key>[0]" (EC2 subnet_id ← the VPC's recorded public_subnet_ids[0]). An attr the
    #   parent never recorded → ask, never guess.
    companion_fields: dict[str, str] = field(default_factory=dict)
    # companion_fields: other child inputs fillable from the SAME parent's recorded attributes,
    # e.g. EKS subnet_ids <- the chosen VPC's "private_subnet_ids" output.


# The real dependency surface of the approved modules (mirrors schemas/workflows.py + the
# modules' variables — never invented fields).
SLOTS: dict[str, list[Slot]] = {
    "aws.eks": [Slot(field="vpc_id", parent_cloud="aws", parent_type="vpc", required=True,
                     creator="aws.vpc",
                     wires={"vpc_id": "vpc_id", "subnet_ids": "private_subnet_ids"},
                     companion_fields={"subnet_ids": "private_subnet_ids"})],
    "aws.nlb": [Slot(field="vpc_id", parent_cloud="aws", parent_type="vpc", required=True,
                     creator="aws.vpc",
                     wires={"vpc_id": "vpc_id", "subnets": "public_subnet_ids"},
                     companion_fields={"subnets": "public_subnet_ids"})],
    "aws.ec2": [Slot(field="subnet_id", parent_cloud="aws", parent_type="vpc", required=False,
                     creator="aws.vpc",
                     wires={"subnet_id": "public_subnet_ids[0]"},
                     value_from="attr:public_subnet_ids[0]",
                     stated_default="the default VPC's subnet (module auto-resolves)")],
    "azure.storage": [Slot(field="resource_group", parent_cloud="azure",
                           parent_type="resource_group", required=True,
                           creator="azure.resource_group", value_from="name",
                           wires={"resource_group": "input:name"})],
    "azure.keyvault": [Slot(field="resource_group", parent_cloud="azure",
                            parent_type="resource_group", required=False,
                            creator="azure.resource_group", value_from="name",
                            wires={"resource_group": "input:name"},
                            stated_default="a module-created “<name>-rg” resource group")],
    "azure.vnet": [Slot(field="resource_group", parent_cloud="azure",
                        parent_type="resource_group", required=False,
                        creator="azure.resource_group", value_from="name",
                        wires={"resource_group": "input:name"},
                        stated_default="a module-created “<name>-rg” resource group")],
    "azure.vm": [Slot(field="resource_group", parent_cloud="azure",
                      parent_type="resource_group", required=False,
                      creator="azure.resource_group", value_from="name",
                      wires={"resource_group": "input:name"},
                      stated_default="a module-created “<name>-rg” resource group")],
    "azure.aks": [Slot(field="resource_group", parent_cloud="azure",
                       parent_type="resource_group", required=False,
                       creator="azure.resource_group", value_from="name",
                       wires={"resource_group": "input:name"},
                       stated_default="a module-created “<name>-rg” resource group")],
    "azure.db": [Slot(field="resource_group", parent_cloud="azure",
                      parent_type="resource_group", required=False,
                      creator="azure.resource_group", value_from="name",
                      wires={"resource_group": "input:name"},
                      stated_default="a module-created resource group")],
    # MS-9: CMEK is OFFERED when a gcp.kms ring exists (its first key's id), never forced —
    # with no ring the field stays empty and Google-managed encryption applies.
    "gcp.cloudsql": [Slot(field="encryption_key_name", parent_cloud="gcp",
                          parent_type="kms", required=False,
                          creator="gcp.kms", value_from="attr:key_ids[0]",
                          wires={},
                          stated_default="Google-managed encryption (no CMEK)")],
    # MS-12 (B4, BY DESIGN): a known gcp.vpc places the VM — "create a vm in prod-network"
    # lands in the EXISTING network, stated on the card; none known → the default network.
    "gcp.vm": [Slot(field="network", parent_cloud="gcp",
                    parent_type="vpc", required=False,
                    creator="gcp.vpc", value_from="name",
                    wires={"network": "input:name"},
                    stated_default="the project's default network")],
}

# "in a NEW vpc" / "with a fresh resource group" — the user explicitly wants the parent
# created, which skips named/world-model/default and forces create-first.
_WANTS_NEW = {
    "vpc": re.compile(r"\b(?:new|fresh|dedicated|its own)\s+(?:vpc|network)\b", re.IGNORECASE),
    "resource_group": re.compile(r"\b(?:new|fresh|dedicated|its own)\s+(?:resource\s*group|rg)\b",
                                 re.IGNORECASE),
}


@dataclass
class Closure:
    """The resolver's decision for one request."""

    status: str                     # complete | ask | dag
    inputs: dict                    # (complete) validated inputs, enriched
    notes: list[str] = field(default_factory=list)      # provenance for the approval card
    question: str = ""              # (ask)
    options: list[dict] = field(default_factory=list)   # (ask) candidate parents
    dag: list[dict] = field(default_factory=list)       # (dag) ordered steps, parents first


def _slot_value(slot: Slot, cand: dict) -> str | None:
    """The slot's value from an existing parent's REAL recorded facts (None = not derivable)."""
    attrs = cand.get("attributes") or {}
    if slot.value_from == "name":
        return cand.get("name")
    if slot.value_from.startswith("attr:"):
        expr = slot.value_from[5:]
        key, indexed = (expr[:-3], True) if expr.endswith("[0]") else (expr, False)
        val = attrs.get(key)
        if indexed:
            return val[0] if isinstance(val, list) and val else None
        return val if isinstance(val, str) and val else None
    return cand.get("provider_id") or cand.get("name")


def _fill_from_candidate(inputs: dict, slot: Slot, cand: dict, notes: list[str]) -> bool:
    """Fill the slot (and its companions) from one existing resource's REAL recorded facts.
    Returns False when the value or a companion can't be derived honestly (caller asks)."""
    value = _slot_value(slot, cand)
    if not value:
        return False  # the parent never recorded what this slot needs — ask, never guess
    attrs = cand.get("attributes") or {}
    inputs[slot.field] = value
    for child_field, attr_key in slot.companion_fields.items():
        if inputs.get(child_field):
            continue  # user already named it
        val = attrs.get(attr_key)
        if not val:
            return False  # can't honestly derive the companion — ask, never guess
        inputs[child_field] = val
    notes.append(f"{slot.field}: using existing {slot.parent_type} "
                 f"“{cand.get('name')}” ({value}) from the world model")
    return True


def _dag_steps(template_key: str, slot: Slot, inputs: dict) -> list[dict]:
    """The ordered create-first plan: parent first, child wired to its outputs."""
    child_name = str(inputs.get("name") or inputs.get("cluster_name")
                     or inputs.get("account_name") or "resource")
    parent_inputs: dict = {"name": f"{child_name}-net" if slot.parent_type == "vpc"
                           else f"{child_name}-rg"}
    for carry in ("region", "location"):
        if inputs.get(carry):
            parent_inputs[carry] = inputs[carry]
    return [
        {"template_key": slot.creator, "inputs": parent_inputs, "provides": slot.parent_type},
        {"template_key": template_key, "inputs": dict(inputs), "wires": dict(slot.wires),
         "depends_on": slot.creator},
    ]


def resolve_closure(template_key: str, validated: dict, active: list[dict],
                    message: str = "") -> Closure:
    """Resolve every dependency slot of `template_key` in the strict order (see module doc).

    `active` is the org's active inventory (`inventory.list_active`); `validated` is the
    schema-validated input dict (mutated copies only — the caller's dict is never touched).
    """
    slots = SLOTS.get(template_key, [])
    inputs = dict(validated)
    notes: list[str] = []

    for slot in slots:
        wants_new = bool(_WANTS_NEW.get(slot.parent_type)
                         and _WANTS_NEW[slot.parent_type].search(message or ""))

        # 1) named — the user already supplied it.
        if not wants_new and inputs.get(slot.field):
            notes.append(f"{slot.field}: “{inputs[slot.field]}” as you named it")
            continue

        candidates = [r for r in active
                      if r.get("cloud") == slot.parent_cloud
                      and r.get("resource_type") == slot.parent_type]

        # 2) world model — one candidate is used (and stated); several are OFFERED, never guessed.
        if not wants_new and len(candidates) == 1:
            if _fill_from_candidate(inputs, slot, candidates[0], notes):
                continue
            cand = candidates[0]
            return Closure(status="ask", inputs=inputs, notes=notes,
                           question=(f"I found {slot.parent_type} “{cand.get('name')}” but its "
                                     f"recorded facts don't include what {template_key} needs "
                                     f"({', '.join(slot.companion_fields)}). Name the values, or "
                                     "say “new” to create a fresh one."),
                           options=[{"name": c.get("name"), "provider_id": c.get("provider_id")}
                                    for c in candidates])
        if not wants_new and len(candidates) >= 2:
            names = ", ".join(f"“{c.get('name')}” ({c.get('provider_id') or 'no id'})"
                              for c in candidates[:6])
            return Closure(status="ask", inputs=inputs, notes=notes,
                           question=(f"Which {slot.parent_type} should this use? You have "
                                     f"{len(candidates)}: {names}. Or say “new” to create one."),
                           options=[{"name": c.get("name"), "provider_id": c.get("provider_id")}
                                    for c in candidates])

        # 3) stated default — the module's declared placement, surfaced honestly.
        if not wants_new and slot.stated_default is not None:
            notes.append(f"{slot.field}: defaulting to {slot.stated_default}")
            continue

        # 4) create-first DAG — parent first, child wired to its outputs (executed by U6).
        log.info("dependency.create_first", template=template_key, parent=slot.parent_type,
                 wants_new=wants_new)
        return Closure(status="dag", inputs=inputs, notes=notes,
                       dag=_dag_steps(template_key, slot, inputs))

    return Closure(status="complete", inputs=inputs, notes=notes)
