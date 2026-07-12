"""Reconciliation engine (D3) — drift + orphan detection against the live cloud.

Compares each ACTIVE inventory resource's recorded attributes with a fresh read-only cloud
read (per-type readers; boto3/google SDKs — never Terraform, never mutation) and raises
org-scoped notifications for three finding kinds:

* **drift** — a curated attribute differs live vs recorded ("someone changed the SG in the
  console");
* **deleted_outside** — the resource no longer exists in the cloud but is still active in the
  inventory;
* **orphan** — a cloud resource tagged `ManagedBy=aegisops` that has NO active inventory row
  (the P14 spend leak: it costs money and nothing tracks it).

Findings are deduplicated (Redis fingerprint, 24h) so the periodic sweep never spams the bell,
and mirrored onto the world-model node (`drift=true` + detail). Readers are registered per
(cloud, resource_type); a type with no reader is SKIPPED and counted — never guessed. All live
readers require their cloud's read credentials; without them the sweep honestly reports
`skipped`. Tests exercise the full pipeline with fake readers; live runs are DLV items.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Protocol

import structlog

from ..db import repositories as repo
from ..db.models import Resource
from ..db.session import session_scope
from ..graph_db import world_model
from ..settings import Settings, get_settings

log = structlog.get_logger(__name__)

#: Sentinel: the resource does not exist in the cloud any more.
MISSING = object()

# Curated drift fields per resource type — only facts we record at apply time and can read
# back cheaply. Comparing everything would drown real drift in provider-computed noise.
DRIFT_FIELDS: dict[str, tuple[str, ...]] = {
    "ec2": ("instance_type", "state", "security_groups"),
    "vm": ("machine_type", "status"),
    "s3": ("versioning",),
    "gcs": ("versioning",),
    "security_group": ("ingress_ports",),
}


class LiveReader(Protocol):
    """Read-only view of one (cloud, resource_type) — discovery/verification only (hard rule)."""

    async def read(self, resource: dict) -> Any:
        """Live attributes for one inventory resource, or MISSING if it no longer exists."""

    async def list_managed(self) -> list[dict]:
        """Live resources tagged ManagedBy=aegisops: [{"provider_id", "name"}] (orphan sweep)."""


class Ec2Reader:
    """boto3 read-only reader for aws/ec2 (mirrors inventory.reconcile's describe)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(settings.aws_access_key_id and settings.aws_secret_access_key)

    def _client(self, region: str | None):
        import boto3
        s = self.settings
        return boto3.client("ec2", aws_access_key_id=s.aws_access_key_id,
                            aws_secret_access_key=s.aws_secret_access_key,
                            aws_session_token=s.aws_session_token or None,
                            region_name=region or s.aws_default_region)

    async def read(self, resource: dict) -> Any:
        import anyio
        if not resource.get("provider_id"):
            return MISSING

        def _describe():
            ec2 = self._client(resource.get("region"))
            try:
                res = ec2.describe_instances(InstanceIds=[resource["provider_id"]])
            except Exception as e:  # noqa: BLE001 — a gone instance raises NotFound
                if "NotFound" in str(e):
                    return None
                raise
            for r in res.get("Reservations", []):
                for inst in r.get("Instances", []):
                    return {"instance_type": inst.get("InstanceType"),
                            "state": (inst.get("State") or {}).get("Name"),
                            "security_groups": sorted(g.get("GroupId", "") for g in
                                                      inst.get("SecurityGroups", []))}
            return None

        live = await anyio.to_thread.run_sync(_describe)
        return MISSING if live is None else live

    async def list_managed(self) -> list[dict]:
        import anyio

        def _list():
            ec2 = self._client(None)
            res = ec2.describe_instances(Filters=[
                {"Name": "tag:ManagedBy", "Values": ["aegisops"]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}])
            out = []
            for r in res.get("Reservations", []):
                for inst in r.get("Instances", []):
                    name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
                    out.append({"provider_id": inst["InstanceId"], "name": name})
            return out

        return await anyio.to_thread.run_sync(_list)


def default_readers(settings: Settings) -> dict[tuple[str, str], LiveReader]:
    """The real reader registry — only clouds whose read creds are configured."""
    readers: dict[tuple[str, str], LiveReader] = {}
    ec2 = Ec2Reader(settings)
    if ec2.enabled:
        readers[("aws", "ec2")] = ec2
    return readers


def detect_drift(resource_type: str, recorded: dict, live: dict) -> list[dict[str, Any]]:
    """Pure comparator: curated fields where the live value differs from the recorded one.

    Only fields the inventory actually recorded are compared — an attribute we never captured
    can't honestly be called drifted. Lists compare order-insensitively.
    """
    diffs: list[dict[str, Any]] = []
    for field in DRIFT_FIELDS.get(resource_type, ()):
        if field not in recorded or field not in live:
            continue
        rec, liv = recorded[field], live[field]
        if isinstance(rec, list) and isinstance(liv, list):
            if sorted(map(str, rec)) == sorted(map(str, liv)):
                continue
        elif rec == liv:
            continue
        diffs.append({"field": field, "recorded": rec, "live": liv})
    return diffs


def _fingerprint(kind: str, org_id: str, ref: str, detail: str) -> str:
    return "drift:fp:" + hashlib.sha1(f"{kind}|{org_id}|{ref}|{detail}".encode()).hexdigest()


async def _notify_once(org_id: str, kind: str, ref: str, title: str, body: str) -> bool:
    """Create the org-scoped notification unless the same finding fired in the last 24h."""
    from ..cache.redis import get_redis

    key = _fingerprint(kind, org_id, ref, body)
    try:
        fresh = await get_redis().set(key, "1", nx=True, ex=86400)
    except Exception as e:  # noqa: BLE001 — a dedup failure must not drop the finding
        log.warning("drift.dedup_unavailable", error=str(e))
        fresh = True
    if not fresh:
        return False
    color = "var(--red)" if kind == "deleted_outside" else "var(--amber)"
    async with session_scope() as s:
        await repo.NotificationRepo.create(s, uuid.UUID(org_id), title=title,
                                           level="warning", color=color, body=body)
    log.info("drift.finding", kind=kind, org_id=org_id, ref=ref, title=title)
    return True


async def _active_inventory(org_id: str | None = None) -> list[dict]:
    from sqlalchemy import select

    async with session_scope() as s:
        q = select(Resource).where(Resource.status == "active")
        if org_id:
            q = q.where(Resource.org_id == uuid.UUID(org_id))
        rows = (await s.execute(q)).scalars().all()
        return [{"org_id": str(r.org_id), "name": r.name, "cloud": r.cloud,
                 "resource_type": r.resource_type, "provider_id": r.provider_id,
                 "region": r.region, "attributes": r.attributes or {}} for r in rows]


async def _annotate(org_id: str, ref: str, detail: str) -> None:
    """World-model drift annotation — best-effort: the NOTIFICATION is the finding; a down
    graph must never abort the sweep or drop findings for the remaining resources."""
    try:
        await world_model.set_drift(org_id, ref, detail)
    except Exception as e:  # noqa: BLE001
        log.warning("drift.annotate_failed", ref=ref, error=str(e))


async def sweep(readers: dict[tuple[str, str], LiveReader] | None = None,
                org_id: str | None = None) -> dict[str, int]:
    """One reconciliation pass over the active inventory (optionally one org's). Returns a
    summary. Sweeps drift + deleted-outside per resource, then the per-cloud orphan listing.
    Bounded: one live read per resource; skipped types are counted, never guessed.
    """
    settings = get_settings()
    readers = default_readers(settings) if readers is None else readers
    summary = {"checked": 0, "drift": 0, "deleted_outside": 0, "orphans": 0, "skipped": 0}

    inventory_rows = await _active_inventory(org_id)
    active_pids = {r["provider_id"] for r in inventory_rows if r["provider_id"]}

    for res in inventory_rows:
        reader = readers.get((res["cloud"], res["resource_type"]))
        if reader is None:
            summary["skipped"] += 1
            continue
        summary["checked"] += 1
        ref = res["provider_id"] or res["name"]
        try:
            live = await reader.read(res)
        except Exception as e:  # noqa: BLE001 — one unreadable resource must not stop the sweep
            log.warning("drift.read_failed", resource=ref, error=str(e))
            continue
        if live is MISSING:
            body = (f"{res['resource_type']} “{res['name']}” ({ref}) no longer exists in "
                    f"{res['cloud']} but is still active in the inventory — it was deleted "
                    "outside AegisOps.")
            if await _notify_once(res["org_id"], "deleted_outside", ref,
                                  f"Deleted outside AegisOps: {res['name']}", body):
                summary["deleted_outside"] += 1
                await _annotate(res["org_id"], ref, body)
            continue
        diffs = detect_drift(res["resource_type"], res["attributes"], live)
        if diffs:
            detail = "; ".join(f"{d['field']}: recorded {d['recorded']!r} → live {d['live']!r}"
                               for d in diffs)
            body = f"Drift on {res['resource_type']} “{res['name']}” ({ref}): {detail}"
            if await _notify_once(res["org_id"], "drift", ref,
                                  f"Drift detected: {res['name']}", body):
                summary["drift"] += 1
                await _annotate(res["org_id"], ref, detail)

    # Orphan sweep (P14): live resources tagged ManagedBy=aegisops with no active inventory row.
    swept_clouds: set[str] = set()
    for (cloud, _rtype), reader in readers.items():
        if cloud in swept_clouds:
            continue
        swept_clouds.add(cloud)
        try:
            managed = await reader.list_managed()
        except Exception as e:  # noqa: BLE001
            log.warning("drift.orphan_list_failed", cloud=cloud, error=str(e))
            continue
        for item in managed:
            pid = item.get("provider_id")
            if not pid or pid in active_pids:
                continue
            # An orphan has no inventory row → no org to notify. For an org-scoped sweep the
            # caller's org owns the finding; otherwise route it to the org that owns ANY
            # inventory in this cloud (single-tenant-per-cloud in practice) — if that's
            # ambiguous, log loudly and skip rather than mis-attribute.
            if org_id:
                owner = org_id
            else:
                org_ids = {r["org_id"] for r in inventory_rows if r["cloud"] == cloud}
                if len(org_ids) != 1:
                    log.warning("drift.orphan_unattributable", cloud=cloud, provider_id=pid,
                                candidate_orgs=len(org_ids))
                    continue
                owner = next(iter(org_ids))
            body = (f"{cloud} resource {pid} ({item.get('name') or 'unnamed'}) is tagged "
                    "ManagedBy=aegisops but has no active inventory record — it is billing "
                    "with nothing tracking it (orphan).")
            if await _notify_once(owner, "orphan", pid, f"Orphaned resource: {pid}", body):
                summary["orphans"] += 1

    if any(summary[k] for k in ("drift", "deleted_outside", "orphans")):
        log.info("drift.sweep_findings", **summary)
    return summary
