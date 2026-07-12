"""Provisioned-resource inventory (Phase 4 — resource state & memory).

Records every resource the platform applies, resolves later references to them ("test-vm",
"the instance I created"), reconciles their live state via read-only cloud SDK calls, and marks
destroyed ones. Everything is org-scoped. This is what gives the agent memory of day-1 work so
it can perform day-2 operations on the real resource instead of a generic account-wide discovery.
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import select

from ..db.models import Resource
from ..db.session import session_scope

log = structlog.get_logger(__name__)

# Which validated-input key holds a resource's stable name, per resource type.
_NAME_KEYS = ("name", "bucket_name", "identifier", "cluster_name", "account_name")
# Which output is the canonical provider id, in priority order.
_ID_KEYS = ("instance_id", "vpc_id", "cluster_name", "bucket_name", "endpoint",
            "storage_account_id", "resource_group_id")
# Descriptive references that mean "the resource I just made".
_RECENT_WORDS = ("instance", "vm", "ec2", "created", "earlier", "just", "last", "it", "new", "above", "previous")

# Broad references that mean "everything I've created" — never a literal name lookup (BUG-04).
_BROAD_EXACT = {"all", "any", "everything", "*", "them", "them all", "all of them", "my resources"}
_BROAD_SHAPE = re.compile(r"\b(?:all|any|every|my)\b[^.?!]*\bresources\b|^resources$", re.IGNORECASE)

# Resource-type words in a descriptive reference → the recorded resource_type values they mean.
# "the s3 bucket I created" must resolve to an s3/gcs/storage resource — never fall back to the
# most-recent resource of a DIFFERENT type (screenshot 5: an S3 question recalled the EC2).
_TYPE_WORDS: list[tuple[tuple[str, ...], set[str]]] = [
    (("instance", "vm", "ec2", "server", "machine", "gce"), {"ec2", "vm"}),
    (("bucket", "s3", "gcs", "blob", "storage"), {"s3", "gcs", "storage"}),
    (("database", "db", "rds", "postgres", "sql"), {"rds", "postgres", "cloudsql"}),
    (("cluster", "eks", "aks", "gke", "k8s", "kubernetes"), {"eks", "aks", "gke"}),
    (("vpc", "network"), {"vpc"}),
]


def _is_broad(ref: str) -> bool:
    r = ref.strip().lower()
    return r in _BROAD_EXACT or bool(_BROAD_SHAPE.search(r))


def is_broad_ref(ref: str | None) -> bool:
    """Public: does this target reference mean "everything I created" rather than one resource?"""
    return bool(ref) and _is_broad(ref)


def _tokens(ref_l: str) -> set[str]:
    """Whitespace-delimited words (punctuation-stripped, naive singular added).

    Deliberately NOT split on hyphens: a name-shaped token like "ghost-server-99" must stay
    one token so its fragments ("server", "it…") can never trigger fuzzy descriptive matching.
    """
    out: set[str] = set()
    for t in ref_l.split():
        t = t.strip(",.?!:;\"'`()")
        if t:
            out.add(t)
            if t.endswith("s") and len(t) > 3:
                out.add(t[:-1])
    return out


def _wanted_types(tokens: set[str]) -> set[str]:
    want: set[str] = set()
    for words, types in _TYPE_WORDS:
        if tokens & set(words):
            want |= types
    return want


def name_from_inputs(inputs: dict, resource_type: str) -> str:
    for k in _NAME_KEYS:
        if inputs.get(k):
            return str(inputs[k])
    return resource_type


def _provider_id(outputs: dict) -> str | None:
    for k in _ID_KEYS:
        if outputs.get(k):
            return str(outputs[k])
    return None


def _dump(r: Resource) -> dict:
    return {"id": str(r.id), "name": r.name, "cloud": r.cloud, "region": r.region,
            "resource_type": r.resource_type, "provider_id": r.provider_id, "workspace": r.workspace,
            "state_workspace": r.state_workspace,
            "status": r.status, "attributes": r.attributes or {}, "inputs": r.inputs or {},
            "created_at": r.created_at.isoformat() if r.created_at else None}


async def record_from_apply(state: dict, template, outputs: dict) -> None:
    """Upsert an inventory row after a successful apply (keyed by org+workspace+name)."""
    inputs = state.get("parsed_inputs") or {}
    name = name_from_inputs(inputs, template.resource)
    org_id = state["org_id"]
    region = inputs.get("region") or inputs.get("location") or state.get("user", {}).get("region")
    try:
        async with session_scope() as s:
            row = (await s.execute(select(Resource).where(
                Resource.org_id == uuid.UUID(org_id), Resource.workspace == template.workspace,
                Resource.name == name, Resource.status == "active"))).scalar_one_or_none()
            if row is None:
                row = Resource(org_id=uuid.UUID(org_id), name=name, cloud=template.cloud,
                               resource_type=template.resource, workspace=template.workspace)
                s.add(row)
            row.session_id = uuid.UUID(state["session_id"]) if state.get("session_id") else None
            row.run_id = uuid.UUID(state["run_id"]) if state.get("run_id") else None
            row.region = region
            row.provider_id = _provider_id(outputs)
            row.attributes = outputs
            row.inputs = inputs
            row.state_workspace = state.get("state_workspace") or row.state_workspace
            row.status = "active"
        log.info("inventory.recorded", name=name, provider_id=_provider_id(outputs), workspace=template.workspace)
    except Exception as e:  # noqa: BLE001 - inventory write must never fail a real apply
        log.warning("inventory.record_failed", error=str(e))
    # Also record in the context graph: resource ↔ run ↔ session relationships (both stores).
    try:
        from ..graph_db.context_graph import ContextGraph
        ctx = state.get("context_id") or state.get("run_id")
        await ContextGraph(ctx, org_id).add_resource(
            name=name, cloud=template.cloud, resource_type=template.resource, provider_id=_provider_id(outputs),
            region=region, run_id=state.get("run_id"), session_id=state.get("session_id"), attributes=outputs)
    except Exception as e:  # noqa: BLE001 - graph write is best-effort, never fails the apply
        log.warning("inventory.graph_record_failed", error=str(e))


async def mark_destroyed(org_id: str, workspace: str, name: str | None = None) -> None:
    try:
        async with session_scope() as s:
            q = select(Resource).where(Resource.org_id == uuid.UUID(org_id),
                                       Resource.workspace == workspace, Resource.status == "active")
            if name:
                q = q.where(Resource.name == name)
            for row in (await s.execute(q)).scalars():
                row.status = "destroyed"
    except Exception as e:  # noqa: BLE001
        log.warning("inventory.mark_destroyed_failed", error=str(e))
    try:  # mirror in the context graph
        from ..graph_db.context_graph import mark_resource_destroyed_graph
        await mark_resource_destroyed_graph(name=name)
    except Exception as e:  # noqa: BLE001
        log.warning("inventory.graph_mark_destroyed_failed", error=str(e))


async def provenance(*, provider_id: str | None = None, name: str | None = None) -> dict | None:
    """Read a resource's provenance (context/run/session) from the graph. Best-effort."""
    try:
        from ..graph_db.context_graph import resource_provenance
        return await resource_provenance(provider_id=provider_id, name=name)
    except Exception as e:  # noqa: BLE001
        log.warning("inventory.provenance_failed", error=str(e))
        return None


async def list_active(org_id: str, clouds: list[str] | None = None) -> list[dict]:
    """Every active resource for the org (optionally filtered by cloud), newest first (BUG-04)."""
    async with session_scope() as s:
        q = select(Resource).where(Resource.org_id == uuid.UUID(org_id),
                                   Resource.status == "active").order_by(Resource.created_at.desc())
        if clouds:
            q = q.where(Resource.cloud.in_([c.lower() for c in clouds]))
        return [_dump(r) for r in (await s.execute(q)).scalars()]


async def resolve(org_id: str, ref: str | None) -> tuple[list[dict], str]:
    """Resolve a reference to inventory. Returns (matches, kind) where kind ∈ all|name|recent|none.

    kind "all" = a broad reference ("all resources", "everything") → every active resource;
    the caller renders a listing (possibly empty). For the other kinds the caller disambiguates
    when len(matches) > 1 and asks the user when 0.
    """
    ref_l = (ref or "").strip().lower()
    # 0) broad reference → the whole active inventory, even when it's empty (the caller renders
    #    an honest "nothing provisioned yet", never the not-found refusal — BUG-04).
    if ref_l and _is_broad(ref_l):
        return await list_active(org_id), "all"
    async with session_scope() as s:
        active = list((await s.execute(select(Resource).where(
            Resource.org_id == uuid.UUID(org_id), Resource.status == "active")
            .order_by(Resource.created_at.desc()))).scalars())
    if not active:
        return [], "none"
    # 1) exact name
    exact = [r for r in active if r.name.lower() == ref_l]
    if exact:
        return [_dump(r) for r in exact], "name"
    # 2) name substring (either direction)
    if ref_l:
        sub = [r for r in active if r.name.lower() in ref_l or ref_l in r.name.lower()]
        if sub:
            return [_dump(r) for r in sub], "name"
    # 3) descriptive ("the instance I created", "the s3 bucket I made") → most-recent active of
    #    the MENTIONED type. Whole-word tokens only: a name-shaped ref ("ghost-server-99") must
    #    never fuzzy-match — its fragments aren't descriptive words. If a type is mentioned and
    #    nothing of that type exists, return none — never a resource of a DIFFERENT type
    #    (screenshot 5 recalled the EC2 for an S3-bucket question).
    tokens = _tokens(ref_l)
    want = _wanted_types(tokens)
    if not ref_l or tokens & set(_RECENT_WORDS) or want:
        cand = [r for r in active if not want or r.resource_type in want]
        if cand:
            return [_dump(cand[0])], "recent"
    return [], "none"


async def reconcile(resource: dict, settings) -> dict:
    """Refresh live status/attributes via a read-only cloud SDK call; persist status changes.

    Returns the resource dict with reconciled attributes + status (terminated resources are marked
    so they aren't offered for day-2 actions).
    """
    attrs = dict(resource.get("attributes") or {})
    status = resource.get("status", "active")

    def _describe_ec2() -> list:
        # Blocking boto3 call — MUST run off the event loop (B6/P6): a cold/regional describe
        # would otherwise stall every concurrent stream in this worker.
        import boto3
        ec2 = boto3.client("ec2", aws_access_key_id=settings.aws_access_key_id,
                           aws_secret_access_key=settings.aws_secret_access_key,
                           aws_session_token=settings.aws_session_token or None,
                           region_name=resource.get("region") or settings.aws_default_region)
        return ec2.describe_instances(InstanceIds=[resource["provider_id"]]).get("Reservations", [])

    try:
        if resource["cloud"] == "aws" and resource["resource_type"] == "ec2" and resource.get("provider_id"):
            import anyio
            resv = await anyio.to_thread.run_sync(_describe_ec2)
            inst = (resv[0]["Instances"][0] if resv and resv[0].get("Instances") else None)
            if inst is None:
                status = "terminated"
            else:
                st = inst.get("State", {}).get("Name", "")
                attrs.update({"public_ip": inst.get("PublicIpAddress"), "private_ip": inst.get("PrivateIpAddress"),
                              "public_dns": inst.get("PublicDnsName"), "state": st,
                              "vpc_id": inst.get("VpcId"), "subnet_id": inst.get("SubnetId")})
                if st in ("terminated", "shutting-down"):
                    status = "terminated"
    except Exception as e:  # noqa: BLE001 - reconciliation is best-effort; fall back to recorded values
        log.warning("inventory.reconcile_failed", error=str(e), provider_id=resource.get("provider_id"))
        return {**resource, "attributes": attrs, "status": status}
    # Persist a status change (e.g. terminated) + refreshed attributes.
    if status != resource.get("status") or attrs != (resource.get("attributes") or {}):
        try:
            async with session_scope() as s:
                row = await s.get(Resource, uuid.UUID(resource["id"]))
                if row:
                    row.status = status
                    row.attributes = attrs
        except Exception as e:  # noqa: BLE001
            log.warning("inventory.reconcile_persist_failed", error=str(e))
    return {**resource, "attributes": attrs, "status": status}
