"""CloudOps agent — multi-cloud provisioning/modify/destroy/read across AWS, Azure, GCP.

Flow: select template (by router's cloud+resource) → extract + Pydantic-validate inputs →
real availability checks (cloud SDK reads) → terraform init/validate/plan → policy + confidentiality
→ build the approval artifact (plan/diff/policy) and INTERRUPT. On approve: terraform apply/destroy
(streamed to the console) → verify via SDK reads → outcome. Cloud SDKs never provision — Terraform
does every mutation, and only after the human-approval gate.
"""

from __future__ import annotations

import json
import re
import uuid

import structlog

from ..db.models import Run
from ..db.session import session_scope
from ..graph_db.context_graph import ContextGraph
from ..llm import service as llm_service
from ..security import idempotency
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools import aws as aws_tool
from ..tools import azure as azure_tool
from ..tools import gcp as gcp_tool
from ..tools.terraform import TerraformError, TerraformRunner, state_slug
from . import cost, dependency, intent_guard, inventory, llm, params, plan_guard, provider_errors, templates, timing
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


# The user's verb decides the DIRECTION of a port change. Before this (forensic audit,
# 2026-08-16), extraction only ever produced ports-to-OPEN: "remove port 8501" was planned
# as "open inbound TCP [8501]", terraform saw no diff, and the run reported applied=true
# while the port stayed open. remove/close/delete/revoke can never become add/open again.
_PORT_CLOSE_VERBS = re.compile(
    r"\b(?:remove|close|delete|revoke|block|drop|disable|shut)\b", re.IGNORECASE)


async def _extract_port_changes(settings, message: str) -> dict:
    """Pull the TCP port changes the user asked for (day-2 SG modify), verb-aware.

    Returns {"open": [ports], "close": [ports]} — only explicitly requested changes."""
    changes: dict = {"open": [], "close": []}

    def _valid(ports) -> list[int]:
        return sorted({int(p) for p in (ports or []) if 0 < int(p) <= 65535})

    if llm_service.configured(settings, "extract"):
        try:
            r = await llm.classify_json(
                settings,
                'Extract the TCP port changes the user explicitly asked for on an existing '
                'resource. Ports to OPEN/allow go in open_ports; ports to CLOSE/remove/'
                'revoke/block go in close_ports. Respond with ONLY JSON: '
                '{"open_ports": [<int>, ...], "close_ports": [<int>, ...]}.', message,
                purpose="extract",
                response_schema={"type": "object", "properties": {
                    "open_ports": {"type": "array", "items": {"type": "integer"}},
                    "close_ports": {"type": "array", "items": {"type": "integer"}}}})
            changes["open"] = _valid(r.get("open_ports"))
            changes["close"] = _valid(r.get("close_ports"))
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.port_extract_failed", error=str(e))
    if not changes["open"] and not changes["close"]:
        # Deterministic fallback: digits following "port(s)", direction from the verb.
        m = re.search(r"ports?\b([\d,\s/and]+)", message, re.IGNORECASE)
        if m:
            ports = _valid(re.findall(r"\d{1,5}", m.group(1)))
            key = "close" if _PORT_CLOSE_VERBS.search(message) else "open"
            changes[key] = ports
    # The DETERMINISTIC verb check always wins over the model: a message whose own words
    # say close/remove (and never open/allow) can only ever close — whatever the LLM said.
    if changes["open"] and _PORT_CLOSE_VERBS.search(message) and not re.search(
            r"\b(?:open|allow|add|permit|expose|enable)\b", message, re.IGNORECASE):
        changes["close"] = sorted(set(changes["close"]) | set(changes["open"]))
        changes["open"] = []
    return changes


async def _extract_modification(settings, message: str) -> dict:
    """MOD: pull the day-2 changes the user asked for. LLM-first with honest regex
    fallbacks; only EXPLICITLY requested changes appear in the result."""
    import re

    changes: dict = {}
    if llm_service.configured(settings, "extract"):
        try:
            r = await llm.classify_json(
                settings,
                'Extract ONLY the modifications the user explicitly asked for on an existing '
                'cloud resource. Respond with ONLY JSON, omitting keys the user did not ask '
                'about: {"ingress_ports": [<int ports to OPEN/allow>], '
                '"ingress_ports_remove": [<int ports to CLOSE/remove/revoke/block>], '
                '"power": "running|stopped", '
                '"versioning": <bool>, "lifecycle_expire_days": <int>, '
                '"instance_class": "<db.x.y>", "allocated_storage": <int GiB>, '
                '"tags": {"<key>": "<value>"}}. '
                '"start/power on" → power=running; "stop/power off/shut down" → power=stopped. '
                'NEVER put a port the user wants removed/closed into ingress_ports.',
                message, purpose="extract")
            for k in ("ingress_ports", "ingress_ports_remove", "power", "versioning",
                      "lifecycle_expire_days", "instance_class", "allocated_storage", "tags"):
                if r.get(k) not in (None, [], {}, ""):
                    changes[k] = r[k]
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.modify_extract_failed", error=str(e))

    low = message.lower()
    if "ingress_ports" not in changes and "ingress_ports_remove" not in changes:
        pc = await _extract_port_changes(settings, message)
        if pc["open"]:
            changes["ingress_ports"] = pc["open"]
        if pc["close"]:
            changes["ingress_ports_remove"] = pc["close"]
    # Deterministic direction guard over the LLM extraction (same rule as
    # _extract_port_changes): close-verbed messages with no open-verb can only close.
    if (changes.get("ingress_ports") and _PORT_CLOSE_VERBS.search(message)
            and not re.search(r"\b(?:open|allow|add|permit|expose|enable)\b", message,
                              re.IGNORECASE)):
        changes["ingress_ports_remove"] = sorted(
            set(changes.get("ingress_ports_remove") or []) | set(changes["ingress_ports"]))
        changes.pop("ingress_ports")
    if "power" not in changes:
        if re.search(r"\b(start|power\s*on|boot|turn\s*on)\b", low):
            changes["power"] = "running"
        elif re.search(r"\b(stop|power\s*off|shut\s*down|turn\s*off)\b", low):
            changes["power"] = "stopped"
    if "versioning" not in changes:
        m = re.search(r"versioning\s*(on|off|enabled?|disabled?|suspend\w*)", low)
        if m:
            changes["versioning"] = m.group(1).startswith(("on", "enable"))
    if "lifecycle_expire_days" not in changes:
        m = re.search(r"(?:expire|lifecycle|delete\s+objects?)\D{0,24}?(\d{1,4})\s*days?", low)
        if m:
            changes["lifecycle_expire_days"] = int(m.group(1))
    if "instance_class" not in changes:
        m = re.search(r"\b(db\.[a-z0-9]+\.[a-z0-9]+)\b", low)
        if m and re.search(r"\b(scale|resize|upgrade|instance class|class)\b", low):
            changes["instance_class"] = m.group(1)
    if "allocated_storage" not in changes:
        m = re.search(r"\b(?:storage|disk)\D{0,16}?(\d{2,5})\s*g[i]?b\b", low)
        if m:
            changes["allocated_storage"] = int(m.group(1))
    if "tags" not in changes:
        m = re.search(r"\btag\s+([A-Za-z][\w.-]*)\s*[=:]\s*([\w.-]+)", message)
        if m:
            changes["tags"] = {m.group(1): m.group(2)}
    if isinstance(changes.get("power"), str):
        changes["power"] = changes["power"].strip().lower()
        if changes["power"] not in ("running", "stopped"):
            changes.pop("power")
    return changes


# MOD capability map: which day-2 changes each resource family supports, and how each
# change lands on the module inputs. Power is Terraform-encoded (owner Option A) — AWS via
# aws_ec2_instance_state, GCE via desired_status; Azure has no TF-native power path.
_MODIFY_CAPS: dict[str, set[str]] = {
    "aws.ec2": {"ingress_ports", "ingress_ports_remove", "power", "tags"},
    "gcp.vm": {"ingress_ports", "ingress_ports_remove", "power"},
    "azure.vm": {"ingress_ports", "ingress_ports_remove"},
    "aws.s3": {"versioning", "lifecycle_expire_days", "tags"},
    "aws.rds": {"instance_class", "allocated_storage", "tags"},
}

_AZURE_POWER_ANSWER = ("Power on/off isn't supported for Azure — use the portal for that; "
                       "create, modify, and destroy are fully supported here.")


def _apply_modification(base: dict, changes: dict) -> tuple[dict, list[str]]:
    """Merge the requested changes onto the stored inputs; return (merged, descriptions).

    Ports compute the DESIRED STATE: opens are unioned, closes are subtracted (a close wins
    over a simultaneous open of the same port — "remove" is the decisive request). Requests
    that change nothing (opening an already-open port, closing a port that isn't open) are
    described honestly so the caller can report NO_CHANGE instead of a phantom apply."""
    merged = dict(base)
    desc: list[str] = []
    if "ingress_ports" in changes or "ingress_ports_remove" in changes:
        current = {int(p) for p in (base.get("ingress_ports") or [])}
        opens = {int(p) for p in (changes.get("ingress_ports") or [])}
        closes = {int(p) for p in (changes.get("ingress_ports_remove") or [])}
        merged["ingress_ports"] = sorted((current | opens) - closes)
        really_opened = sorted(opens - current - closes)
        already_open = sorted((opens & current) - closes)
        really_closed = sorted(closes & (current | opens))
        not_open = sorted(closes - current - opens)
        if really_opened:
            desc.append(f"open inbound TCP {really_opened}")
        if really_closed:
            desc.append(f"close inbound TCP {really_closed}")
        if already_open:
            desc.append(f"port(s) {already_open} already open — nothing to do there")
        if not_open:
            desc.append(f"port(s) {not_open} not open — nothing to close there")
    if "power" in changes:
        merged["power_state"] = changes["power"]
        desc.append(f"set power state to {changes['power']} (Terraform-managed, no SDK call)")
    if "versioning" in changes:
        merged["versioning"] = bool(changes["versioning"])
        desc.append(f"turn versioning {'on' if changes['versioning'] else 'off'}")
    if "lifecycle_expire_days" in changes:
        merged["lifecycle_expire_days"] = int(changes["lifecycle_expire_days"])
        desc.append(f"expire objects after {changes['lifecycle_expire_days']} days")
    if "instance_class" in changes:
        merged["instance_class"] = str(changes["instance_class"])
        desc.append(f"scale to {changes['instance_class']}")
    if "allocated_storage" in changes:
        merged["allocated_storage"] = int(changes["allocated_storage"])
        desc.append(f"grow storage to {changes['allocated_storage']} GiB")
    if "tags" in changes:
        merged["extra_tags"] = {**(base.get("extra_tags") or {}), **dict(changes["tags"])}
        desc.append("update tags " + ", ".join(f"{k}={v}" for k, v in changes["tags"].items()))
    return merged, desc


async def _live_ingress_ports(settings, res: dict) -> list[int] | None:
    """OBSERVE-BEFORE-ACT (Prompt 3): the MANAGED app ports actually open on the resource's
    security group, read from the live cloud. Only rules this platform manages are counted
    (description-tagged "AegisOps app port …") — the admin SSH/RDP rule and the sentinel
    self-rule are separate concerns and never leak into `ingress_ports`. None = could not
    inspect (no SG recorded / provider unreachable) → the caller falls back to recorded
    state, stated honestly."""
    sg_id = (res.get("attributes") or {}).get("security_group_id")
    if not sg_id or res.get("cloud") != "aws":
        return None

    def _describe() -> list[int]:
        import boto3
        ec2 = boto3.client("ec2", aws_access_key_id=settings.aws_access_key_id,
                           aws_secret_access_key=settings.aws_secret_access_key,
                           aws_session_token=settings.aws_session_token or None,
                           region_name=res.get("region") or settings.aws_default_region)
        sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        ports: set[int] = set()
        for perm in sg.get("IpPermissions", []):
            fp, tp = perm.get("FromPort"), perm.get("ToPort")
            if fp is None or fp != tp:
                continue
            descriptions = [r.get("Description", "") for r in perm.get("IpRanges", [])]
            if any(d.startswith("AegisOps app port") for d in descriptions):
                ports.add(int(fp))
        return sorted(ports)

    try:
        import anyio
        return await anyio.to_thread.run_sync(_describe)
    except Exception as e:  # noqa: BLE001 — inspection is best-effort, never blocks a modify
        log.warning("cloudops.live_sg_inspect_failed", sg=sg_id, error=str(e))
        return None


# STAB P1-1: identity fields where the user's literal token is the only honest value.
_NAME_FIELDS = {"name", "bucket_name", "identifier", "account_name", "cluster_name"}


async def _extract_inputs(settings, template, message: str, *, org_id: str | None = None,
                          user_id: str | None = None) -> dict:
    """Extract this module's parameter values from NL via Gemini, merged with free-form parsing.

    M4: "my usual region" resolves DETERMINISTICALLY from the user's standing memory — no LLM
    required — and an explicit region in the message still wins."""
    from ..schemas.workflows import parse_freeform

    inputs = parse_freeform(message)
    if org_id and re.search(r"\busual\s+(?:region|location)\b", message or "", re.IGNORECASE):
        from . import user_memory
        usual = await user_memory.lookup(org_id, user_id, "usual_region")
        if usual:
            field = "location" if template.cloud == "azure" else "region"
            inputs.setdefault(field, usual)
            log.info("cloudops.usual_region_honored", region=usual, field=field)
    if llm_service.configured(settings, "extract"):
        fields = params.extraction_fields(template.key) or ", ".join(template.schema.model_fields.keys())
        system = (
            "Extract provisioning parameter values from the user's message for a Terraform module. "
            f"Look for these fields (use the exact names as JSON keys): {fields}. "
            "Respond with ONLY a JSON object; omit any field not present in the message. "
            "Normalize OS synonyms: 'ubuntu'->'ubuntu-22.04', 'amazon linux'/'al2023'->'amazon-linux-2023', "
            "'windows'->'windows-2022'. If the user asks to create/generate a key pair, use \"create\". "
            "For allowed_cidr: an IP the user wants access from (bare IP is fine, e.g. 203.0.113.7); "
            "'none'/'closed'/'keep it closed'/'skip' -> \"none\". Lists as JSON arrays."
        )
        try:
            extracted = await llm.classify_json(settings, system, message, purpose="extract")
            clean = {k: v for k, v in extracted.items() if v not in (None, "")}
            # STAB P1-1: a NAME is never invented or silently "fixed". The live case:
            # the reply `mybucket-sai@22042792002` was extracted as the DIFFERENT (valid)
            # name `mybucket-sai-22042792002` and planned without a word. If the model's
            # value for a name-like field isn't literally in the message, drop it — the
            # params ask then states the naming rule and the user decides. Choice fields
            # (os, engine, …) are exempt: synonym normalization there is the desired
            # behavior and is guarded by their allowlist validators instead.
            for k in list(clean):
                if (k in _NAME_FIELDS and isinstance(clean[k], str)
                        and clean[k].strip().strip("\"'`").lower() not in (message or "").lower()):
                    log.warning("cloudops.extracted_name_not_verbatim",
                                field=k, value=clean[k])
                    clean.pop(k)
            inputs = {**clean, **inputs}  # explicit free-form key=value wins over the LLM
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.extract_failed", error=str(e))
    return inputs


def _invalid_fields(exc: Exception) -> list[tuple[str, str]]:
    """Field-level messages from a Pydantic ValidationError (for a specific clarification)."""
    try:
        return [(str(e.get("loc", ["input"])[0]), e.get("msg", "invalid")) for e in exc.errors()]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return [("input", str(exc))]


# Resource vocabulary → cloud hint (fallback only, when neither the request nor the UI names a
# cloud). ONLY cloud-branded service names are hinted (S3/RDS/VPC/EKS→aws, Storage/Resource
# Group→azure, GCS→gcp). Compute/"VM"/"ec2" is deliberately NOT hinted: a VM request with no
# named cloud is genuinely cross-cloud, so it must ask rather than silently defaulting to AWS.
_CLOUDS = {"aws", "azure", "gcp"}
_RESOURCE_CLOUD = {"s3": "aws", "rds": "aws", "vpc": "aws", "eks": "aws",
                   "resource_group": "azure", "storage": "azure", "gcs": "gcp"}


def _defaulted_dependencies(cloud: str, resource: str, validated: dict, resources: list | None) -> list[dict]:
    """DEF: dependency placements that were NOT user-specified and got a default — stated
    explicitly for the approval card so there is no invisible placement decision. The value is the
    resolved id from the plan where available, else an honest description of the default."""
    out: list[dict] = []
    r = (resource or "").lower()
    if cloud == "aws" and r in ("ec2", "vm", "instance", "server"):
        # A filled subnet_id is a REAL placement (user-named or DEP-resolved — the closure
        # note states its provenance); only a genuinely-defaulted placement is flagged here.
        # (Was keyed on vpc_id, which no input carries — so the card claimed "account's
        # default VPC" even when the resolver had bound a named VPC. Audit 2026-08-17.)
        if not validated.get("subnet_id"):
            inst = next((x.get("after", {}) for x in (resources or []) if x.get("type") == "aws_instance"), {})
            sub = inst.get("subnet_id")
            out.append({"name": "VPC / subnet", "value": sub or "account default VPC + subnet",
                        "note": "no VPC specified — placing in the account's default VPC"})
    elif cloud == "gcp" and r in ("vm", "instance", "gce", "server"):
        # MS-12 (B4, by design): a slot-filled/user-named network is a REAL placement — the
        # DEP closure states its provenance; only the default placement is flagged here.
        if (validated.get("network") or "default") == "default":
            out.append({"name": "Network", "value": "default",
                        "note": "no network specified — placing in the project's 'default' VPC network"})
    elif cloud == "azure" and r in ("vm", "instance", "server"):
        if not validated.get("resource_group"):
            out.append({"name": "Resource group", "value": f"{validated.get('name', '')}-rg (auto-created)",
                        "note": "no resource group specified — a dedicated one is auto-created"})
    return out


def display_region(cloud: str, inputs: dict | None, ui_region: str | None) -> str:
    """STAB P2-2: the region/location shown beside a cloud is that CLOUD'S OWN — from the
    user's inputs when given, else the cloud's default — never the UI context's AWS-style
    region (live: "Queried AZURE · us-east-1", "Queried GCP · us-east-1", screenshots 5/7)."""
    inputs = inputs or {}
    if cloud == "azure":
        return str(inputs.get("location") or "eastus")
    if cloud == "gcp":
        return str(inputs.get("region") or "us-central1")
    return str(inputs.get("region") or ui_region or "us-east-1")


def resolve_cloud(state: AgentState) -> tuple[str | None, str]:
    """Resolve the target cloud with explicit priority (2.1):

    (a) a cloud named in the request (router-extracted), (b) the UI cloud selector, (c) a hint
    from the resource vocabulary. If still ambiguous, return (None, reason) so the agent asks
    the user — it must NEVER silently default to AWS.
    """
    explicit = (state.get("cloud") or "").lower()
    if explicit in _CLOUDS:
        return explicit, "named in request"
    ui = (state.get("user", {}).get("cloud") or "").lower()
    if ui in _CLOUDS:
        return ui, "UI cloud selector"
    hint = _RESOURCE_CLOUD.get((state.get("resource") or "").lower())
    if hint:
        return hint, f"inferred from resource '{state.get('resource')}'"
    return None, "ambiguous"


async def _availability(settings, cloud: str, region: str, emitter) -> dict:
    """Real read-only availability/connectivity pre-check via the matching cloud SDK."""
    from ..integrations.langfuse_client import get_tracer

    async with get_tracer(settings).tool(f"{cloud}.availability",
                                         input={"cloud": cloud, "region": region}) as t:
        result = await _availability_inner(settings, cloud, region)
        t.output = result
    return result


async def _availability_inner(settings, cloud: str, region: str) -> dict:
    try:
        if cloud == "aws":
            r = aws_tool.get_aws(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "AWS credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "AWS reachable (STS)", "passed": ok, "detail": region}]}
        if cloud == "azure":
            r = azure_tool.get_azure(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "Azure credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "Azure reachable", "passed": ok}]}
        if cloud == "gcp":
            r = gcp_tool.get_gcp(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "GCP credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "GCP reachable", "passed": ok}]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "checks": [{"name": f"{cloud} availability", "passed": False, "detail": str(e)[:160]}]}
    return {"available": True, "checks": []}


# ── COMP: honest handling of compound / attach-mount / OS-change asks (Phase-3 exit item).
# The chatbot must NEVER silently pick one resource from a multi-resource ask, pretend to
# mount storage it can't configure in-guest, or fake an in-place OS swap.
_COMP_CATEGORIES: dict[str, tuple[str, ...]] = {
    "compute": ("virtual machine", "ubuntu vm", "windows vm", "linux vm", " vm", "vm ",
                "instance", "ec2", "gce", "compute engine"),
    "storage": ("gcs bucket", "s3 bucket", " bucket", "blob", "object storage",
                "storage account", "storage bucket"),
    "network": ("vpc", "vnet", "virtual network", "subnet"),
    "database": ("database", " rds", "cloud sql", "cloudsql", "postgres", "mysql",
                 "mariadb", "sql server", "mssql"),
    "kms": ("kms", "key ring", "keyring", "key vault", "keyvault", " vault", "crypto key"),
    "loadbalancer": ("load balancer", "load-balancer", "loadbalancer", " nlb", " alb", " elb"),
    "kubernetes": (" eks", " aks", " gke", "kubernetes", "k8s", " cluster"),
    "resource_group": ("resource group",),
}
# Dependency linkage → the DEP create-first DAG owns it (e.g. "an EC2 inside a new VPC",
# DLV-12). COMP compound-refuse must NEVER fire on these.
_DEP_LINK = re.compile(
    r"\b(inside|within|into|attached to|attach to|connected to)\b"
    r"|\bin (?:a|an|the|my|its?|this|that|new|existing)\b", re.IGNORECASE)
_ATTACH = re.compile(r"\b(attach|mount|connect)\b", re.IGNORECASE)
_OS_CHANGE = re.compile(
    r"\b(change|switch|convert|swap|migrate|make|reinstall|set)\b[^.]*\b"
    r"(os|operating system|image|to (?:windows|ubuntu|debian|amazon[- ]linux|rhel|centos))\b",
    re.IGNORECASE)
_OS_TOKENS = re.compile(r"\b(windows|ubuntu|debian|amazon[- ]linux|rhel|centos)\b", re.IGNORECASE)


# BUGFIX-3 (live acceptance run 2): "instance"/"server" directly qualifying a database noun
# is that DATABASE's own noun phrase — Cloud SQL/RDS products are literally named
# "instances" ("a postgres cloudsql instance", "an rds instance", "a sql server"). Counting
# the qualifier as compute made ONE resource look like a compound ask. Collapse the phrase
# to its database head before category detection; a free-standing "instance" still means
# compute ("an instance and a bucket" stays compound).
_DB_QUALIFIED = re.compile(
    r"\b(cloud\s*sql|cloudsql|rds|database|db|postgres(?:ql)?|mysql|mariadb|"
    r"sql\s*server|mssql)((?:[\s-]+(?:instance|server))+)\b", re.IGNORECASE)


def _detected_categories(message: str) -> list[str]:
    collapsed = _DB_QUALIFIED.sub(r"\1", message.lower())
    low = f" {collapsed} "
    hits: list[str] = []
    for cat, kws in _COMP_CATEGORIES.items():
        if any(k in low for k in kws):
            hits.append(cat)
    return hits


def _comp_intercept(state: AgentState) -> dict | None:
    """Return an honest answer dict for a compound / attach / OS-change ask, else None to
    fall through to the normal single-resource flow (which includes the DEP DAG path)."""
    import re as _re

    message = state.get("message", "") or ""
    action = (state.get("action") or "create").lower()

    def _answer(msg: str, *, clarify: bool = False) -> dict:
        cc = classify(msg)
        base = {"needs_change": False, "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}
        if clarify:
            base.update({"needs_clarification": True, "clarification": msg})
        else:
            base["approval_status"] = "not_required"
        return base

    # (c) OS change on an existing VM — a modify that would REPLACE, not update in place.
    if action == "modify" and _OS_CHANGE.search(message):
        target = state.get("target") or "that instance"
        cloud = (state.get("cloud") or "").lower()
        note = ("  Note: this Linux module (gcp.vm) has no Windows image — Windows VMs live "
                "on **azure.vm** or **aws.ec2**.\n" if cloud == "gcp"
                or "gcp" in message.lower() or "gce" in message.lower() else "")
        return _answer(
            f"I can't change the operating system of **{target}** in place — swapping the OS "
            "replaces the instance (a new boot disk + machine), which my safety guard treats "
            "as a destroy, not a modify. Honest path: I can **destroy** it (gated approval) "
            "and **create a fresh VM** on the OS you want, reusing the same name and network.\n"
            f"{note}Want me to start the gated destroy + recreate?")

    # (b) attach / mount storage onto a VM — creation is offered, but MOUNTING is in-guest
    # configuration AegisOps does not perform. Never a pretend attach, never cross-cloud wiring.
    if _ATTACH.search(message) and any(
            k in f" {message.lower()} " for k in _COMP_CATEGORIES["storage"]):
        return _answer(
            "I can **create** the storage (a bucket/blob container) as its own governed "
            "resource, but I can't **attach/mount** it onto a VM for you — mounting object "
            "storage is *in-guest* configuration that runs on the instance itself, outside "
            "AegisOps' Terraform-only mutation scope. After I create it, mount it on the VM "
            "with a one-liner:\n"
            "• GCS → `gcsfuse <bucket> /mnt/<dir>`\n"
            "• S3 → `s3fs <bucket> /mnt/<dir> -o iam_role=auto`\n"
            "• Azure Blob → `blobfuse2 mount /mnt/<dir> --container-name=<c>`\n"
            "Want me to create the bucket now? I won't wire a fake attachment.")

    # (b2) STAB P1-5: attach a DATABASE to an instance (live screenshot 21: «Create a mysql
    # db in the aws and attach the Sai-test-v1» — the attach half was silently dropped and
    # only the DB planned). A managed database is *connected to*, never attached: the honest
    # decomposition is (1) create the DB, (2) day-2 modify scoping its allowed CIDR/SG to
    # that instance, (3) the app's connection string does the rest. Half a request is never
    # silently discarded.
    if (_ATTACH.search(message)
            and any(k in f" {message.lower()} " for k in _COMP_CATEGORIES["database"])
            # "connect the DB to my vpc/subnet" is a private-access placement ask, not an
            # instance attach — the network/DEP paths own it.
            and not any(k in f" {message.lower()} " for k in _COMP_CATEGORIES["network"])):
        return _answer(
            "I can **create** the database as its own governed resource, but “attach it to "
            "an instance” isn't one wired step here — a managed database is *connected to*, "
            "not attached. The honest path: **(1)** create the database now (its connection "
            "string + credentials arrive on the success card, revealed once), **(2)** as a "
            "day-2 modify I can scope the DB's allowed source (its security group / allowed "
            "CIDR) to that instance, **(3)** point the app on the instance at the connection "
            "string — in-guest client setup stays outside my Terraform scope.\n"
            "Want me to start with the database create? I won't silently drop the attach half.",
            clarify=True)

    # (a) compound INDEPENDENT resources (no dependency linkage → not a DEP DAG). Offer to do
    # them one at a time; never silently pick one.
    if action == "create" and not _DEP_LINK.search(message):
        cats = _detected_categories(message)
        has_conj = bool(_re.search(r"\band\b|,|\bplus\b|\balso\b|&", message, _re.IGNORECASE))
        if len(cats) >= 2 and has_conj:
            pretty = {"compute": "the VM", "storage": "the storage bucket", "network": "the network",
                      "database": "the database", "kms": "the key", "loadbalancer": "the load balancer",
                      "kubernetes": "the cluster", "resource_group": "the resource group"}
            ordered = [pretty.get(c, c) for c in cats]
            first = ordered[0]
            return _answer(
                "That's a **compound request** for "
                f"{len(cats)} independent resources ({', '.join(ordered)}). I create resources "
                "one at a time so each gets its own plan + approval — I won't silently pick just "
                f"one. Suggested order: {', then '.join(ordered)}. "
                f"Shall I start with **{first}**? (For resources that genuinely depend on each "
                "other — e.g. “an EC2 inside a new VPC” — I do build the ordered plan in one "
                "approval; this ask looks independent.)",
                clarify=True)
    return None


async def cloudops_plan(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    action = state.get("action") or "create"
    region = state.get("user", {}).get("region", "us-east-1")
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))

    # ── Day-2 operations on an EXISTING resource (Phase 4): resolve it from inventory.
    # A BROAD target ("all") is not a single-resource lookup — it falls through to the
    # discovery path below, which answers with live per-cloud counts AND the full inventory
    # listing (Phase 7 / BUG-04).
    target = state.get("target")
    if action == "read" and target and not inventory.is_broad_ref(target):
        return await _read_resource(state, config, target)
    # COMP: intercept compound / attach-mount / OS-change asks with an honest answer BEFORE
    # the flow commits to a single resource (or an in-place modify). Read paths are exempt.
    if action in ("create", "modify"):
        comp = _comp_intercept(state)
        if comp is not None:
            await emitter.step(2, "Compound/attach/OS-change — answering honestly")
            await emitter.token(comp["answer"])
            cc = comp["confidentiality"]
            await emitter.confidentiality(cc["level"], cc["score"])
            return comp
    if action == "modify":
        return await _modify_resource(state, config, target)
    # Destroy is a DAY-2 operation on an inventoried resource — it resolves its target from
    # the inventory, confirms it via the approval gate, and tears down that resource's OWN
    # state workspace. It never enters the create-style parameter collection (Phase 8 / N-08:
    # the old path collected create-params and destroyed whatever shared state contained).
    if action == "destroy":
        return await _destroy_resource(state, config, target)

    # ── Read path: discovery only, no Terraform. Runs BEFORE cloud resolution because a
    # read-only question must never hard-fail on cloud ambiguity — it can answer across every
    # cloud the question names (or all configured ones). (Phase 7 / BUG-01, BUG-04.)
    if action == "read":
        await timing.start_step(run_id, "cloudops_agent")
        r = await _read_path(state, config)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return r

    # 2.1 — resolve the target cloud with explicit priority; never silently default to AWS.
    cloud, cloud_reason = resolve_cloud(state)
    if cloud is None:
        await emitter.step(2, "Cloud ambiguous — asking user")
        return {"needs_clarification": True, "needs_change": False,
                "clarification": "Which cloud should I target — **AWS**, **Azure**, or **GCP**? "
                                 "I couldn't tell from your request or the workspace cloud selector."}
    resource = (state.get("resource") or "").lower()
    log.info("cloudops.cloud_resolved", cloud=cloud, reason=cloud_reason, resource=resource, run_id=run_id)
    await emitter.step(2, f"Target cloud · {cloud.upper()} ({cloud_reason})")

    # 2.2 — select the cloud-specific template. No cross-cloud fallback: if this cloud has no
    # approved module for the resource, clarify honestly (don't provision on a different cloud).
    template = templates.select(cloud, resource)
    if template is None:
        await emitter.step(3, f"No approved {cloud}/{resource or '?'} module")
        supported = ", ".join(sorted(f"{t.cloud}/{t.resource}" for t in templates.TEMPLATES))
        return {"needs_clarification": True, "needs_change": False, "cloud": cloud,
                "clarification": f"I resolved the target cloud to **{cloud.upper()}**, but there's no approved "
                                 f"Terraform module for **{cloud}/{resource or 'that resource'}** yet, so I can't plan "
                                 f"it — and I won't provision it on another cloud. Currently supported: {supported}."}

    await emitter.step(3, f"Selected workflow · {template.key} {template.version}")

    # ── Interactive parameter collection (3.1–3.3) — CREATE only: read/modify/destroy have
    # all returned above, so this flow can only ever produce a create plan (Phase 8 / N-08).
    session_id = state.get("session_id")
    pending_rec: dict = {}
    if session_id:
        rec = await params.load_pending(session_id) or {}
        # Same-template guard: a pending record from a DIFFERENT workflow must never leak
        # its values into this one (defensive — pending is keyed per session, not template).
        if rec and rec.get("template") in (None, template.key):
            # Continue an in-progress collection (existing behavior), OR reuse the inputs a
            # FAILED apply preserved (forensic-audit remediation, 2026-08-16: "retry with
            # t2.micro" used to forget OS/key/subnet from the attempt seconds earlier).
            if state.get("collecting") or rec.get("after_failure"):
                pending_rec = rec

    await timing.start_step(run_id, "cloudops_agent")

    def _pending_record(collected: dict) -> dict:
        return {"template": template.key, "cloud": cloud, "resource": resource, "action": "create",
                "intent": state.get("intent"), "collected": collected,
                "snow_id": state.get("snow_id"), "context_id": context_id}

    prior: dict = pending_rec.get("collected", {})
    extracted = await _extract_inputs(settings, template, state["message"],
                                      org_id=state.get("org_id"),
                                      user_id=state.get("user", {}).get("user_id"))
    collected = {**prior, **{k: v for k, v in extracted.items() if v not in (None, "", [])}}
    # GCP resources default the project to the configured one (never asked).
    if cloud == "gcp" and not collected.get("project"):
        collected["project"] = settings.google_cloud_project

    # Missing decision-critical params → ask for exactly those (never for defaulted VPC/subnet).
    # STAB P2-4: DEP-slot-covered fields (EKS vpc_id/subnet_ids, NLB vpc_id, RG names…) are
    # NEVER demanded as raw ids on the params card — the dependency closure below fills them
    # from the world model, asks with the real candidates, or drafts the create-first DAG
    # (live: the EKS card demanded "Existing VPC id (vpc-…)" + subnet ids, screenshots 18-19).
    dep_fields = dependency.slot_fields(template.key)
    missing = [m for m in params.missing_required(template.key, collected)
               if m.name not in dep_fields]
    if missing:
        req = params.request_payload(template.key, collected)
        msg = params.summary_text(template.key, collected)
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        await emitter.params(req)
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "param_request": req,
                "parsed_inputs": collected, "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── DEP: dependency closure (strict order: named → world model → stated default →
    # create-first DAG). Runs BEFORE schema validation (STAB P2-4): slot-covered required
    # fields (EKS vpc_id/subnet_ids…) are filled here from the world model — the schema then
    # validates the ENRICHED inputs, so the user is never asked for raw provider ids.
    # An ambiguous parent ASKS with the real candidates; a missing required parent yields an
    # ordered create-first plan (executed by the executive loop, U6).
    # BUGFIX-2 (live acceptance run 2): when the PREVIOUS turn was that ask, map this turn's
    # reply ("new" / a candidate's name — exactly the forms the ask suggests) back onto the
    # slot; before this, the reply was re-resolved from scratch and the ask repeated forever.
    dep_choice = dependency.choice_from_reply(state.get("message", ""),
                                              pending_rec.get("dep_ask"))
    if dep_choice:
        await emitter.step(4, f"Placement answered · {dep_choice['parent_type']} → "
                              f"{'a new one' if dep_choice['choice'] == '__new__' else dep_choice['choice']}")
    closure = dependency.resolve_closure(
        template.key, collected,
        await inventory.list_active(state["org_id"]), message=state.get("message", ""),
        dep_choice=dep_choice)
    if closure.status == "ask":
        if session_id:
            rec = _pending_record(collected)
            # persist WHICH slot asked + the real candidates, so the next turn's bare reply
            # can be mapped honestly instead of being re-classified (BUGFIX-2)
            rec["dep_ask"] = {"parent_type": closure.parent_type, "options": closure.options}
            await params.save_pending(session_id, rec)
        await emitter.step(4, "Placement is ambiguous — asking")
        await emitter.token(closure.question)
        cc = classify(closure.question)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": closure.question,
                "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}
    if closure.status == "dag":
        if settings.aegisops_exec_loop == "on":
            # U6: hand the create-first DAG to the Governed Executive Loop — per-step plans +
            # ONE whole-DAG approval; execution happens post-approval in the execute node.
            from . import exec_loop
            return await exec_loop.plan_goal_dag(state, config, closure.dag)
        steps_txt = " → ".join(f"{i+1}) {s['template_key']}"
                               f" “{s['inputs'].get('name') or s['inputs'].get('cluster_name') or s['inputs'].get('account_name') or ''}”"
                               for i, s in enumerate(closure.dag))
        msg = (f"**{template.key}** needs a {closure.dag[0]['provides']} that doesn't exist yet. "
               f"Ordered plan (parents first): {steps_txt} — the child is wired to the parent's "
               "real outputs. Multi-step execution runs through the governed executive loop; "
               "each plan is applied only after your approval.")
        await emitter.step(4, "Create-first plan drafted (dependency closure)")
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "goal_dag": closure.dag, "cloud": cloud, "resource": resource,
                "confidentiality": {"level": cc.level, "score": cc.score}}
    collected = closure.inputs

    # All required present → validate the concrete Terraform variables against the module schema.
    if session_id:
        await params.clear_pending(session_id)
    try:
        validated = template.schema(**params.to_tf_vars(template.key, collected)).model_dump()
    except Exception as e:  # noqa: BLE001 - Pydantic ValidationError → per-field clarification, no plan
        bad = _invalid_fields(e)
        labels = {p.name: p.label for p in params.specs_for(template.key)}
        for fname, _ in bad:
            collected.pop(fname, None)  # drop the invalid value so it's re-asked specifically
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        detail = "; ".join(f"**{labels.get(f, f)}** — {m}" for f, m in bad) or str(e)
        msg = f"That value didn't validate: {detail}. Please send a valid value."
        await emitter.params(params.request_payload(template.key, collected))
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="failed", error=str(e))
        await cg.add_step(order=1, name="validate_inputs", agent="cloudops", tool="pydantic", status="failed")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── S3 names are GLOBALLY unique — read-only HeadBucket precheck so the user isn't burned
    # by a 409 at apply time (Phase 7 / BUG-07). Best-effort: unknown ⇒ proceed to plan.
    if template.key == "aws.s3" and aws_tool.get_aws(settings).enabled:
        taken = await aws_tool.get_aws(settings).bucket_taken(validated["bucket_name"])
        if taken:
            bad_name = validated["bucket_name"]
            collected.pop("bucket_name", None)
            if session_id:
                await params.save_pending(session_id, _pending_record(collected))
            msg = (f"The bucket name **`{bad_name}`** is already taken — S3 bucket names are "
                   "globally unique across ALL AWS customers, so common names are long gone. "
                   "Pick a more distinctive one (e.g. prefix it with your org or project, "
                   "like `acme-payments-logs`) and send it over.")
            await emitter.step(4, f"Bucket name “{bad_name}” is taken — asking for another")
            await emitter.params(params.request_payload(template.key, collected))
            await emitter.token(msg)
            cc = classify(msg)
            await emitter.confidentiality(cc.level, cc.score)
            await timing.end_step(run_id, "cloudops_agent", status="done")
            return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                    "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                    "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── Availability (real SDK reads) ──
    # P2-2: the label carries the CLOUD'S OWN region/location (from the validated inputs),
    # never the UI context's AWS-style default.
    region_label = display_region(cloud, validated, region)
    await emitter.step(4, f"Queried {cloud.upper()} · {region_label}")
    avail = await _availability(settings, cloud, region_label, emitter)
    for c in avail["checks"]:
        await emitter.console("stdout", f"availability: {c['name']} = {'ok' if c['passed'] else 'n/a'} {c.get('detail','')}")
    await timing.end_step(run_id, "cloudops_agent", status="done")

    # ── Terraform plan ── (fixed reviewed module; user inputs passed strictly as -var)
    workspace = template.workspace
    tf_vars = validated

    # Per-resource state isolation (Phase 8 / N-08): every create plans/applies inside its
    # OWN Terraform workspace, so it can never reconcile — and destroy/replace — a resource
    # created earlier in the same module ("create deleted my previous instance").
    res_name = inventory.name_from_inputs(validated, template.resource)
    tf_state_ws = state_slug(res_name)

    # Same-name loophole (Senior Reviewer): an identical name would map to the SAME state
    # workspace and re-share state. A create never reuses an active resource's name.
    dup = [m for m in await inventory.list_active(state["org_id"])
           if m["name"] == res_name and m["workspace"] == template.workspace]
    if dup:
        collected.pop(next((p.name for p in params.specs_for(template.key)
                            if p.name in ("name", "bucket_name", "identifier", "cluster_name",
                                          "account_name")), "name"), None)
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        msg = (f"You already have an active resource named **{res_name}** (created "
               f"{(dup[0].get('created_at') or '')[:16]}). A create never touches an existing "
               "resource — pick a different name, or say "
               f"“destroy {res_name}” first if you want to replace it.")
        await emitter.step(4, f"Name “{res_name}” already active — asking for another")
        await emitter.params(params.request_payload(template.key, collected))
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # (P2-4: the DEP closure now runs BEFORE schema validation — see above. The inputs
    # here are already slot-enriched and schema-validated.)
    # MODSEED: environment-aware defaults (e.g. NLB deletion_protection ON for Production) —
    # resolved after validation, stated on the card, never silent; explicit choices win.
    env_notes = templates.apply_env_defaults(template.key, validated,
                                             state.get("user", {}).get("env"))
    tf_vars = validated

    mode = "apply"
    await emitter.step(4, f"Ran terraform plan · state {tf_state_ws}")
    await timing.start_step(run_id, "planner", tool="terraform")
    runner = TerraformRunner(workspace, settings, state_workspace=tf_state_ws, run_id=run_id)

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(tf_vars, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        # Classify the provider failure and explain it in plain English (Phase 7 / BUG-05) —
        # the raw trace stays in the Logs tab; the conversation gets cause + next step.
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="plan")
        # U7: one-click retry-with-fix — a genuine new turn with only the fix applied.
        retry = provider_errors.suggest_retry(failure, state.get("message", ""), cloud=cloud,
                                              current_region=validated.get("region")
                                              or validated.get("location") or region)
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error",
                            retriable=True, retry=retry)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        await cg.add_step(order=2, name="terraform_plan", agent="cloudops", tool="terraform", status="failed")
        await cg.update_step(order=2, status="failed", error=str(e))
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None,
                            "retry": retry}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # Action-vs-operation HARD GUARD (Phase 8 / N-08): the plan about to reach the approval
    # gate must be a pure create. Any destroy/replace in a create plan is blocked here —
    # whatever the classifier or Terraform state did.
    violation = plan_guard.check_plan_actions("create", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="create",
                  summary=plan["summary"])
        await emitter.step(9, "Safety guard · create plan contained destroys — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        await emitter.token(violation)
        cc = classify(violation)
        await emitter.confidentiality(cc.level, cc.score)
        await cg.add_step(order=2, name="plan_guard", agent="cloudops", tool="plan_guard", status="failed")
        return {"needs_change": False, "approval_status": "not_required", "answer": violation,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "blocked_by_guard", "error": violation}}

    # ZERO-CHANGE GUARD (idempotency, forensic-audit remediation 2026-08-16): a create whose
    # plan is 0/0/0 means the desired state already exists in this workspace (a repeated
    # request reconciled cleanly). Honest NO_CHANGE — never a phantom "applied".
    if plan_guard.zero_change(plan["summary"]):
        msg = (f"**{res_name} already matches the requested state** — the Terraform plan is "
               "0 add / 0 change / 0 destroy, so there is nothing to create. No approval "
               "needed; nothing was applied. (A repeated create reconciles instead of "
               "duplicating.)")
        log.info("cloudops.create_no_change", run_id=run_id, name=res_name)
        await emitter.step(4, f"No change needed — {res_name} already satisfied")
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "cloud": cloud, "resource": resource,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "no_change", "resolution": msg}}

    await timing.start_step(run_id, "policy_evaluation")
    policy_checks = template.policy_fn(validated, runner.planned_resources())  # U1: over the real plan
    policy_checks = policy_checks + cost.checks_for(template.key, validated)  # COST: estimate + guardrail on the card
    await timing.end_step(run_id, "policy_evaluation", status="done",
                          result={"passed": sum(1 for p in policy_checks if p["passed"]), "total": len(policy_checks)})
    # DEF: surface any silently-defaulted dependency placement on the approval card — plus the
    # DEP resolver's provenance ("using existing vpc … from the world model").
    defaults = _defaulted_dependencies(cloud, template.resource, validated, runner.planned_resources())
    defaults += [{"name": "Dependency resolution", "value": n, "note": ""} for n in closure.notes]
    defaults += [{"name": "Environment default", "value": n, "note": ""} for n in env_notes]
    plan_json = {"summary": plan["summary"], "diff": plan["diff"], "workspace": template.workspace,
                 "policy_checks": policy_checks, "mode": mode, "state_workspace": tf_state_ws,
                 "defaults": defaults}

    # Confidentiality over the plan + inputs.
    c = classify(json.dumps({"inputs": validated, "plan": plan["summary"]}))
    await emitter.confidentiality(c.level, c.score)

    reasoning = [
        {"title": "Interpreted intent", "conf": f"{int(state.get('intent_confidence', 0) * 100)}%",
         "body": f"{action} {template.resource} on {cloud} — {state.get('routing_reason','')}"},
        {"title": "Workflow selection", "conf": "",
         "body": f"Selected {template.key} {template.version} ({template.description}). Curated, org-approved module."},
        {"title": "Plan", "conf": "",
         "body": f"+{plan['summary']['add']} ~{plan['summary']['change']} -{plan['summary']['destroy']}; "
                 f"{sum(1 for p in policy_checks if p['passed'])}/{len(policy_checks)} policy checks passed."},
    ]
    await emitter.analysis(summary=f"Drafted a Terraform plan for {template.key}; awaiting approval.", cards=reasoning)

    # Context graph
    try:
        await cg.set_workflow(workflow=template.key, version=template.version, template=template.workspace or "generic", inputs=validated)
        await cg.add_step(order=2, name="terraform_plan", agent="cloudops", tool="terraform", status="done")
        await cg.update_step(order=2, status="done", result=plan["summary"])
        await cg.add_evidence(kind="terraform_plan", ref=template.key, detail=plan["summary"])
    except Exception as e:  # noqa: BLE001
        log.warning("cloudops.cg_failed", error=str(e))

    interrupt_payload = {
        "kind": "approval", "runId": state["run_id"], "workflow": template.key,
        "plan": plan_json, "diff": plan["diff"], "policyChecks": policy_checks,
        "mode": mode, "cloud": cloud, "resource": template.resource, "defaults": defaults,
    }
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)

    return {
        "workflow": template.key, "workflow_version": template.version,
        "cloud": cloud, "resource": resource, "state_workspace": tf_state_ws,
        "parsed_inputs": validated, "plan_json": plan_json, "diff": plan["diff"],
        "policy_checks": policy_checks, "dependencies": avail["checks"],
        "execution_mode": mode, "needs_change": True, "approval_status": "pending",
        "confidentiality": {"level": c.level, "score": c.score},
        "reasoning_cards": reasoning, "interrupt_payload": interrupt_payload,
        "answer": f"Drafted a {template.key} plan (+{plan['summary']['add']} ~{plan['summary']['change']} "
                  f"-{plan['summary']['destroy']}). Review and approve to apply.",
    }


# Which resource kind a read question is about (normalizes per-cloud synonyms).
_READ_KINDS = {
    "vm": {"ec2", "vm", "gce", "instance", "compute", "server", "machine"},
    "storage": {"s3", "gcs", "storage", "bucket", "blob", "object_storage", "storage_account"},
    "db": {"rds", "database", "db", "postgres", "postgresql", "cloudsql", "sql", "mysql"},
    "k8s": {"eks", "aks", "gke", "k8s", "kubernetes", "cluster"},
    "network": {"vpc", "network", "subnet"},
}
_CLOUD_WORDS = {"aws": r"\baws\b|\bamazon\b", "azure": r"\bazure\b", "gcp": r"\bgcp\b|\bgoogle\b"}


def _read_kind(resource: str) -> str:
    r = (resource or "").lower()
    return next((k for k, words in _READ_KINDS.items() if r in words), "any")


async def _discover_aws(settings, kind: str, region: str) -> list[str]:
    r = aws_tool.get_aws(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        inst = await r.list_instances(region)
        running = [i for i in inst if i.get("state") == "running"]
        out.append(f"{len(running)} running EC2 instance(s)"
                   + (f" ({', '.join(i['name'] or i['id'] for i in running[:5])})" if running else "")
                   + (f" of {len(inst)} total" if len(inst) != len(running) else ""))
    if kind in ("storage", "any"):
        buckets = await r.list_buckets()
        out.append(f"{len(buckets)} S3 bucket(s)"
                   + (f" ({', '.join(b['name'] for b in buckets[:5])}{'…' if len(buckets) > 5 else ''})" if buckets else ""))
    if kind in ("db", "any"):
        out.append(f"{len(await r.list_databases(region))} RDS instance(s)")
    if kind == "k8s":
        out.append(f"{len(await r.list_eks_clusters(region))} EKS cluster(s)")
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_vpcs(region))} VPC(s)")
    return out


async def _discover_azure(settings, kind: str) -> list[str]:
    r = azure_tool.get_azure(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        vms = await r.list_vms()
        out.append(f"{len(vms)} VM(s)"
                   + (f" ({', '.join(v['name'] for v in vms[:5])})" if vms else ""))
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_vnets())} VNet(s)")
    if kind in ("storage", "db", "k8s") and not out:
        out.append(f"live {kind} listing isn't wired for Azure yet — the AegisOps inventory below covers what I created")
    return out


async def _discover_gcp(settings, kind: str) -> list[str]:
    r = gcp_tool.get_gcp(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        inst = await r.list_all_instances()
        running = [i for i in inst if (i.get("status") or "").upper() == "RUNNING"]
        out.append(f"{len(running)} running Compute instance(s)"
                   + (f" ({', '.join(i['name'] for i in running[:5])})" if running else "")
                   + (f" of {len(inst)} total" if len(inst) != len(running) else ""))
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_networks())} network(s)")
    if kind in ("storage", "db", "k8s") and not out:
        out.append(f"live {kind} listing isn't wired for GCP yet — the AegisOps inventory below covers what I created")
    return out


async def _read_path(state, config) -> dict:
    """Read-only account discovery — real SDK reads, no Terraform, no approval needed.

    Multi-cloud aware (Phase 7): answers for every cloud named in the question, else the
    resolved/selected cloud, else every configured cloud. Counts the resource kind actually
    asked about (instances/buckets/databases/clusters/VPCs), and always includes what the
    AegisOps inventory recorded — never a destructive or provisioning workflow.
    """
    emitter = emitter_of(config)
    settings = get_settings()
    message = (state.get("message") or "").lower()
    region = state.get("user", {}).get("region", "us-east-1")
    kind = _read_kind(state.get("resource"))

    # History/provenance question with no single resolvable resource → answer org-wide from
    # the immutable journal ("what did I change yesterday?", "who approved recent changes?").
    raw_message = state.get("message", "")
    if intent_guard.is_history_question(raw_message) or intent_guard.is_provenance_question(raw_message):
        hist = await _history_answer(state["org_id"], raw_message)
        if hist:
            await emitter.step(3, "Answering from the immutable change journal")
            await emitter.token(hist)
            c = classify(hist)
            await emitter.confidentiality(c.level, c.score)
            await emitter.analysis(
                summary="Answered from the immutable resource-revision journal joined with the "
                        "approvals table — deterministic audit data, no model-generated history.",
                cards=[{"title": "Audit journal read", "conf": "", "body": "org-wide history"}])
            return {"needs_change": False, "approval_status": "not_required", "answer": hist,
                    "confidentiality": {"level": c.level, "score": c.score}}

    # Clouds the QUESTION names scope the whole answer — including the inventory listing
    # below (forensic-audit remediation, 2026-08-16: an "in AWS" question listed GCP rows).
    named_clouds = [c for c, pat in _CLOUD_WORDS.items() if re.search(pat, message)]
    clouds = list(named_clouds)
    if not clouds:
        resolved, _why = resolve_cloud(state)
        clouds = [resolved] if resolved else [c for c, tool in
                  (("aws", aws_tool.get_aws(settings)), ("azure", azure_tool.get_azure(settings)),
                   ("gcp", gcp_tool.get_gcp(settings))) if tool.enabled] or ["aws"]

    await emitter.step(3, f"Querying {', '.join(c.upper() for c in clouds)} · read-only")
    sections: list[str] = []
    failed_clouds: set[str] = set()   # P1-2c: their inventory rows must read UNVERIFIED
    for cloud in clouds:
        try:
            found = await (_discover_aws(settings, kind, region) if cloud == "aws"
                           else _discover_azure(settings, kind) if cloud == "azure"
                           else _discover_gcp(settings, kind))
            sections.append(f"**{cloud.upper()}**: " + ", ".join(found))
            for f in found:
                await emitter.console("stdout", f"discovery[{cloud}]: {f}")
        except Exception as e:  # noqa: BLE001 - one cloud failing must not sink the others
            log.warning("cloudops.discovery_failed", cloud=cloud, error=str(e))
            failed_clouds.add(cloud)
            f = provider_errors.classify_provider_error(str(e))
            sections.append(f"**{cloud.upper()}**: discovery failed — "
                            + (f"{f.title}. {f.next_step}" if f else str(e)[:140]))

    # What AegisOps itself provisioned (the inventory) — always part of the answer. For a
    # broad "did I create any resources" question, render the full grouped listing.
    try:
        # Listing honesty: rows whose AWS resource no longer exists in the CURRENT account
        # (rotated sandbox) are marked `unreachable` before rendering — live cloud overrides
        # stale memory (best-effort, read-only, bounded).
        if "aws" in clouds and not failed_clouds:
            try:
                await inventory.verify_aws_liveness(state["org_id"], settings)
            except Exception as le:  # noqa: BLE001
                log.warning("cloudops.liveness_verify_failed", error=str(le))
        mine = await inventory.list_active(state["org_id"], clouds=clouds)
        if inventory.is_broad_ref(state.get("target")) or intent_guard.is_broad_inventory_question(message):
            # Broad = everything created — but a question that NAMES clouds is scoped to
            # them ("resources I created in AWS" must never list GCP rows). Partial rows
            # (failed applies with leftover state) are shown honestly, never hidden.
            broad = await inventory.list_active(state["org_id"], clouds=named_clouds or None,
                                                statuses=("active", "partial"))
            sections.append("\n" + _render_inventory_list(broad, unverified_clouds=failed_clouds))
        elif mine:
            # P1-2c: a row on a cloud whose live discovery just failed is not "active" as far
            # as anyone can verify right now — say so inline, never imply it was checked.
            names = ", ".join(
                f"{m['name']} ({m['cloud']} {m['resource_type']})"
                + (" ⚠ unverified" if m["cloud"] in failed_clouds else "")
                for m in mine[:8])
            sections.append(f"**Provisioned by AegisOps**: {len(mine)} active — {names}")
        else:
            sections.append("**Provisioned by AegisOps**: none recorded for these clouds")
    except Exception as e:  # noqa: BLE001
        log.warning("cloudops.inventory_list_failed", error=str(e))

    text = "\n".join(sections)
    await emitter.token(text)
    c = classify(text)
    await emitter.confidentiality(c.level, c.score)
    await emitter.analysis(
        summary="Read-only discovery via cloud SDK reads plus the AegisOps inventory — no "
                "infrastructure was changed and no approval was needed.",
        cards=[{"title": "Read-only query", "conf": "",
                # P2-2: only the AWS reader is region-scoped — never stamp an AWS-style
                # region on an Azure/GCP query (live: "Queried AZURE · us-east-1").
                "body": f"kind={kind} · clouds={', '.join(clouds)}"
                        + (f" · aws region={region}" if "aws" in clouds else "")}])
    return {"needs_change": False, "approval_status": "not_required", "answer": text,
            "confidentiality": {"level": c.level, "score": c.score}}


def _render_inventory_list(matches: list[dict], unverified_clouds: set[str] | frozenset = frozenset()) -> str:
    """Inventory listing as a real markdown TABLE (STAB P1-2a). The old version joined
    “• ”-prefixed lines with single newlines — markdown collapses those into the one dense
    paragraph the owner saw live (screenshots 9/17). `unverified_clouds` (P1-2c): clouds
    whose live discovery just FAILED — their rows must say so, never read as verified."""
    if not matches:
        return ("I haven't provisioned any resources in this workspace yet — nothing is recorded "
                "in the inventory. (Failed applies leave no active resource, and destroyed ones "
                "are removed from this list.) Ask me to create something — e.g. "
                "“create an EC2 instance in AWS”.")
    lines = [f"I've provisioned **{len(matches)}** recorded resource(s) "
             "(see Status — partial rows are failed applies with leftover state):", "",
             "| Name | Type | Cloud | Region | Created | Id | Status |",
             "|---|---|---|---|---|---|---|"]
    for m in sorted(matches, key=lambda x: (x.get("cloud") or "", x.get("name") or "")):
        created = (m.get("created_at") or "")[:16].replace("T", " ") or "—"
        pid = f"`{m['provider_id']}`" if m.get("provider_id") else "—"
        status = ("⚠ unverified — live discovery failed"
                  if (m.get("cloud") or "") in unverified_clouds else (m.get("status") or "active"))
        lines.append(f"| **{m.get('name')}** | {m.get('resource_type')} | {m.get('cloud')} "
                     f"| {m.get('region') or '—'} | {created} | {pid} | {status} |")
    lines += ["", "Ask about any of them by name for full details (IPs, VPC, ports, …)."]
    return "\n".join(lines)


# Deterministic time-window words for history questions ("what did I change yesterday?").
_TIME_WORDS = (("yesterday", 1, 1), ("today", 0, 0), ("this week", 7, 0), ("last week", 14, 7))


def _history_window(message: str) -> tuple | None:
    """(since, until) datetimes for a time word in the message, else None (no time filter)."""
    from datetime import datetime, time, timedelta, timezone
    low = (message or "").lower()
    for word, back_start, back_end in _TIME_WORDS:
        if word in low:
            today = datetime.now(timezone.utc).date()
            start = datetime.combine(today - timedelta(days=back_start), time.min, timezone.utc)
            end = datetime.combine(today - timedelta(days=back_end), time.max, timezone.utc)
            return (start, end)
    return None


def _fmt_revision(rev, approval) -> str:
    """One immutable revision → one honest timeline line (never LLM-generated)."""
    when = rev.created_at.strftime("%Y-%m-%d %H:%M UTC") if rev.created_at else "—"
    line = (f"| {when} | **{rev.action}** | {rev.name} ({rev.cloud} {rev.resource_type}) "
            f"| {rev.actor_user or '—'} ")
    detail = ""
    b = (rev.before_state or {}).get("attributes") or (rev.before_state or {})
    a = (rev.after_state or {}).get("attributes") or (rev.after_state or {})
    if isinstance(b, dict) and isinstance(a, dict) and (b.get("ingress_ports") is not None
                                                        or a.get("ingress_ports") is not None):
        detail = f"ports {b.get('ingress_ports') or []} → {a.get('ingress_ports') or []}"
    elif rev.action == "modified" and isinstance(b, dict) and isinstance(a, dict):
        changed = [k for k in set(b) | set(a) if b.get(k) != a.get(k)]
        detail = "changed: " + ", ".join(sorted(changed)[:6]) if changed else ""
    elif rev.action in ("partial", "failed"):
        detail = (rev.reason or "")[:80]
    appr = (f"{approval.decision} by {approval.actor_user} "
            f"{approval.ts.strftime('%H:%M UTC') if approval.ts else ''}" if approval else "—")
    run_short = str(rev.run_id)[:8] if rev.run_id else "—"
    return line + f"| {appr} | {detail or '—'} | `{run_short}` |"


async def _history_answer(org_id: str, message: str, name: str | None = None) -> str | None:
    """Answer a history/provenance question from the immutable audit record — the
    resource_revisions journal joined with the approvals table (forensic-audit remediation,
    2026-08-16: 'who approved / what changed / previous configuration' previously fell into
    generic discovery, a devops misroute, or a raw LLM guess). Deterministic; returns None
    only when the journal read itself fails (the caller falls back honestly)."""
    from ..db.models import Approval, ResourceRevision
    from sqlalchemy import select as _select
    try:
        async with session_scope() as s:
            q = _select(ResourceRevision).where(ResourceRevision.org_id == uuid.UUID(org_id))
            if name:
                q = q.where(ResourceRevision.name == name)
            window = _history_window(message)
            if window:
                q = q.where(ResourceRevision.created_at >= window[0],
                            ResourceRevision.created_at <= window[1])
            revs = list((await s.execute(
                q.order_by(ResourceRevision.created_at.desc()).limit(15))).scalars())
            approvals: dict = {}
            run_ids = [r.run_id for r in revs if r.run_id]
            if run_ids:
                for ap in (await s.execute(_select(Approval).where(
                        Approval.run_id.in_(run_ids)))).scalars():
                    approvals[ap.run_id] = ap
    except Exception as e:  # noqa: BLE001 - an audit read failure must be visible, not guessed over
        log.warning("cloudops.history_read_failed", error=str(e))
        return None
    scope = f"**{name}**" if name else "your resources"
    window = _history_window(message)
    when_note = (f" between {window[0].date()} and {window[1].date()}" if window else "")
    if not revs:
        return (f"No recorded changes for {scope}{when_note}. The immutable change journal "
                "starts at its introduction (2026-08-16) — changes made before that exist "
                "only as run records. Nothing matched this question.")
    lines = [f"Change history for {scope}{when_note} (from the immutable audit journal, "
             f"newest first — {len(revs)} event(s)):", "",
             "| When | Action | Resource | By | Approval | Detail | Run |",
             "|---|---|---|---|---|---|---|"]
    lines += [_fmt_revision(r, approvals.get(r.run_id)) for r in revs]
    lines += ["", "Every row is an append-only revision written in the same transaction as "
                  "the change itself; approvals come from the immutable approvals table."]
    return "\n".join(lines)


async def _read_resource(state: AgentState, config, target: str) -> dict:
    """Day-2 READ: return a specific inventoried resource's real recorded values (reconciled)."""
    emitter = emitter_of(config)
    settings = get_settings()
    org_id = state["org_id"]
    await emitter.step(2, f"Looking up “{target}” in inventory")
    matches, kind = await inventory.resolve(org_id, target)

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # Broad inventory question ("did I create any resources…") → list EVERYTHING active,
    # grouped by cloud — never the literal-match refusal (Phase 7 / BUG-04, screenshots 14/16).
    if kind == "all":
        await emitter.step(3, f"Inventory · {len(matches)} active resource(s)")
        await emitter.analysis(
            summary="Listed every active resource recorded in the AegisOps inventory for this "
                    "workspace (successful applies only; failed/destroyed runs leave no active resource).",
            cards=[{"title": "Inventory listing", "conf": "", "body": f"{len(matches)} active resource(s)"}])
        return await _say(_render_inventory_list(matches))

    if not matches:
        # A history/provenance question about something not in the CURRENT inventory may
        # still have journal rows (e.g. a destroyed or failed resource) — check before refusing.
        message = state.get("message", "")
        if intent_guard.is_history_question(message) or intent_guard.is_provenance_question(message):
            hist = await _history_answer(org_id, message, name=target)
            if hist:
                await emitter.step(3, "Answering from the immutable change journal")
                return await _say(hist)
        return await _say(f"I couldn't find a resource matching “{target}” in what I've provisioned for you. "
                          "Tell me its exact name, or create it first — I won't guess.")
    if len(matches) > 1:
        return await _say(f"More than one resource matches “{target}”: "
                          f"{', '.join(m['name'] for m in matches)}. Which one do you mean?")

    # History/provenance question about THIS resource → the immutable audit record answers
    # (who/when/what changed/previous state), never the current-state card and never the LLM.
    message = state.get("message", "")
    if intent_guard.is_history_question(message) or intent_guard.is_provenance_question(message):
        hist = await _history_answer(org_id, message, name=matches[0]["name"])
        if hist:
            await emitter.step(3, f"Change history of {matches[0]['name']} · audit journal")
            await emitter.analysis(
                summary="Answered from the immutable resource-revision journal joined with the "
                        "approvals table — deterministic audit data, no model-generated history.",
                cards=[{"title": "Audit journal read", "conf": "",
                        "body": f"resource {matches[0]['name']}"}])
            return await _say(hist)

    res = await inventory.reconcile(matches[0], settings)
    await emitter.step(3, f"Recalled {res['name']} · {res.get('provider_id') or ''}")
    attrs = res.get("attributes") or {}
    inputs = res.get("inputs") or {}
    lines = [f"**{res['name']}** — {res['cloud']} {res['resource_type']}"
             + (f" (`{res['provider_id']}`)" if res.get("provider_id") else "") + ":"]
    # Size/type comes from the validated inputs the resource was created with (asked-for
    # attribute in screenshot 6 — "instance size" — was previously never surfaced).
    size = inputs.get("instance_type") or inputs.get("machine_type") or inputs.get("size") or inputs.get("tier")
    if size:
        lines.append(f"• Size / type: `{size}`")
    if res.get("region"):
        lines.append(f"• Region: `{res['region']}`")
    for label, key in [("VPC", "vpc_id"), ("Subnet", "subnet_id"), ("Private IP", "private_ip"),
                       ("Public IP", "public_ip"), ("Public DNS", "public_dns"),
                       ("Security group", "security_group_id"), ("Key pair", "key_name"), ("State", "state")]:
        if attrs.get(key):
            lines.append(f"• {label}: `{attrs[key]}`")
    if attrs.get("ingress_ports"):
        lines.append(f"• Open inbound ports: {attrs['ingress_ports']}")
    if res.get("status") != "active":
        lines.append(f"• ⚠️ Status: {res['status']}")
    # Relationships come from the context graph (never inferred): who provisioned it.
    prov = await inventory.provenance(provider_id=res.get("provider_id"), name=res["name"])
    if prov and (prov.get("run_id") or prov.get("session_id")):
        lines.append(f"• Provisioned by run `{str(prov.get('run_id') or '')[:8]}`"
                     + (f" in session `{str(prov.get('session_id'))[:8]}`" if prov.get("session_id") else "")
                     + " (context graph)")
    msg = "\n".join(lines)
    await emitter.token(msg)
    cc = classify(msg)
    await emitter.confidentiality(cc.level, cc.score)
    await emitter.analysis(
        summary=f"Recalled “{res['name']}” from the provisioned-resource inventory (matched by {kind}); "
                "these are this resource's real recorded attributes, not a generic account discovery.",
        cards=[{"title": "Resolved resource", "conf": "",
                "body": f"{res['name']} · {res.get('provider_id')} · workspace {res['workspace']}"}])
    return {"needs_change": False, "approval_status": "not_required", "answer": msg,
            "cloud": res["cloud"], "resource": res["resource_type"],
            "confidentiality": {"level": cc.level, "score": cc.score}}


async def _world_model_impact_check(org_id: str, *, provider_id: str | None,
                                    name: str | None) -> tuple[dict, list[dict]]:
    """(policy-check row, dependents) for the destroy card — D3 impact gate.

    passed=True only when the world model was actually consulted and found no active dependent;
    dependents → passed=False with a named detail; an unreachable graph → evaluated=False
    (pending), never a silent pass."""
    from ..graph_db import world_model
    try:
        dependents = await world_model.impact_of(org_id, provider_id=provider_id, name=name)
    except Exception as e:  # noqa: BLE001 — the graph being down must not fake a pass
        log.warning("cloudops.impact_check_unavailable", error=str(e))
        return ({"name": "No dependent resources (world model)", "passed": None,
                 "evaluated": False, "detail": "world model unreachable — not evaluated"}, [])
    if dependents:
        detail = ", ".join(f"{d['name'] or d['provider_id']} ({d['type'] or d['kind']})"
                           for d in dependents[:6])
        return ({"name": "No dependent resources (world model)", "passed": False,
                 "evaluated": True,
                 "detail": f"{len(dependents)} active dependent(s): {detail}"}, dependents)
    return ({"name": "No dependent resources (world model)", "passed": True,
             "evaluated": True, "detail": "no active dependents"}, [])


async def _destroy_resource(state: AgentState, config, target: str | None) -> dict:
    """Day-2 DESTROY (Phase 8 / N-08): resolve the target from the INVENTORY, confirm it via
    the approval gate, and tear down that resource's OWN Terraform state workspace.

    Never enters parameter collection, never plans in a shared state, and the plan is hard-
    guarded to contain only delete actions. Resources AegisOps didn't create are refused
    honestly (we won't guess at infrastructure we don't own)."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    org_id = state["org_id"]
    message = state.get("message", "")

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # Destroy always requires the user's own destructive wording — belt-and-suspenders under
    # the router guard (a mirror-redirected destroy carries the verb by construction).
    if not intent_guard.explicitly_destructive(message):
        return await _say("This came through as a **destroy**, but your message doesn't explicitly "
                          "ask to tear anything down, so I stopped — nothing was changed. If you do "
                          "want it destroyed, say so explicitly (e.g. “destroy the VM sai-test”).")

    ref = (target or "").strip()
    # U7: "__last_applied__" — the undo fast-path's target. Resolve to the most recent ACTIVE
    # resource THIS conversation applied; nothing applied here → refuse honestly.
    if ref == "__last_applied__":
        last = await inventory.last_applied(org_id, state.get("session_id"))
        if not last:
            return await _say("There's nothing to undo — this conversation hasn't applied any "
                              "infrastructure that is still active. Name a resource explicitly "
                              "if you want something torn down.")
        ref = last["name"]
        await emitter.console("stdout", f"undo → last applied in this conversation: {ref}")
    await emitter.step(2, f"Resolving “{ref or state.get('resource') or 'resource'}” for destruction")
    # Partial rows (failed applies that left Terraform state) ARE destroyable — that's the
    # designed recovery for a wedged workspace (forensic-audit remediation, 2026-08-16).
    matches, kind = await inventory.resolve(org_id, ref or (state.get("resource") or ""),
                                            statuses=("active", "partial"))
    if kind == "all":
        names = ", ".join(m["name"] for m in matches[:8]) or "none recorded"
        return await _say("I don't bulk-destroy in one shot — that's how accidents happen. "
                          f"Tell me exactly which resource to tear down. Active: {names}.")
    if not matches:
        return await _say(f"I couldn't find “{ref or 'that resource'}” among the resources I've "
                          "provisioned, so there's nothing I can safely destroy. I only tear down "
                          "infrastructure I created (it's tracked in my inventory with its own "
                          "Terraform state).")
    if len(matches) > 1:
        return await _say("More than one resource matches: "
                          f"{', '.join(m['name'] for m in matches)}. Which one should I destroy?")
    res = matches[0]
    if kind == "recent":
        # Fuzzy reference ("the vm I created") — only proceed when it's genuinely unambiguous.
        same_type = [m for m in await inventory.list_active(org_id)
                     if m["resource_type"] == res["resource_type"]]
        if len(same_type) > 1:
            return await _say(f"You have {len(same_type)} {res['resource_type']} resources: "
                              f"{', '.join(m['name'] for m in same_type)}. "
                              "Name the one to destroy — I won't guess on a teardown.")

    template = templates.select(res["cloud"], res["resource_type"])
    if template is None:
        return await _say(f"I can't destroy {res['name']} — its module ({res['cloud']}/"
                          f"{res['resource_type']}) is no longer in the approved catalog.")

    await emitter.step(3, f"Planning teardown of {res['name']}"
                          + (f" · state {res['state_workspace']}" if res.get("state_workspace") else ""))
    await timing.start_step(run_id, "cloudops_agent")
    await timing.end_step(run_id, "cloudops_agent", status="done")
    await timing.start_step(run_id, "planner", tool="terraform")
    runner = TerraformRunner(res["workspace"] or template.workspace, settings,
                             state_workspace=res.get("state_workspace"), run_id=run_id)
    base = dict(res.get("inputs") or {})
    try:
        tf_vars = template.schema(**base).model_dump()
    except Exception:  # noqa: BLE001 - schema drift must never block a teardown
        tf_vars = base

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(tf_vars, destroy=True, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="destroy plan")
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error", retriable=True)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # HARD GUARD: a destroy plan may contain only deletes (Phase 8 / N-08).
    violation = plan_guard.check_plan_actions("destroy", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="destroy", summary=plan["summary"])
        await emitter.step(9, "Safety guard · destroy plan contained creates — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        return await _say(violation)
    if plan["summary"]["destroy"] == 0:
        return await _say(f"Terraform found nothing to tear down for **{res['name']}** — its state "
                          "is already empty (it may have been destroyed outside AegisOps). I've left "
                          "everything untouched; say “is it created?” to reconcile its status.")

    # D3: world-model impact gate — "what depends on this?" answered from the real dependency
    # edges before the human decides. Dependents make the check FAIL on the card (the human can
    # still approve — but never unwarned). Best-effort: an unreachable graph yields an honest
    # not-evaluated check, never a silent pass.
    impact_check, dependents = await _world_model_impact_check(
        org_id, provider_id=res.get("provider_id"), name=res["name"])
    plan_json = {"summary": plan["summary"], "diff": plan["diff"],
                 "workspace": res["workspace"] or template.workspace,
                 "state_workspace": res.get("state_workspace"),
                 "policy_checks": [impact_check], "mode": "destroy"}
    cc = classify(f"destroy {res['name']}")
    await emitter.confidentiality(cc.level, cc.score)
    reasoning = [{"title": "Teardown target", "conf": "",
                  "body": f"{res['name']} · {res['cloud']} {res['resource_type']} · "
                          f"{res.get('provider_id') or 'no provider id'} — resolved from the inventory; "
                          f"plan destroys {plan['summary']['destroy']} resource(s), creates none."}]
    if template.destroy_note:
        # MODSEED: the module's honest deletion semantics, stated where the human decides.
        reasoning.append({"title": "Deletion semantics", "conf": "", "body": template.destroy_note})
    if dependents:
        dep_list = ", ".join(f"{d['name'] or d['provider_id']} ({d['type'] or d['kind']})"
                             for d in dependents[:6])
        reasoning.append({"title": "⚠ Dependent resources (world model)", "conf": "",
                          "body": f"Destroying {res['name']} impacts {len(dependents)} active "
                                  f"resource(s) that depend on it: {dep_list}. They may break "
                                  "or be orphaned."})
        await emitter.console("stderr",
                              f"world-model impact: {len(dependents)} active dependent(s) — {dep_list}")
    await emitter.analysis(summary=f"Planned the teardown of {res['name']}; awaiting approval.", cards=reasoning)
    interrupt_payload = {"kind": "approval", "runId": state["run_id"], "workflow": template.key,
                         "plan": plan_json, "diff": plan["diff"], "policyChecks": [],
                         "mode": "destroy", "cloud": res["cloud"], "resource": res["resource_type"]}
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)
    return {"workflow": template.key, "workflow_version": template.version, "cloud": res["cloud"],
            "resource": res["resource_type"], "state_workspace": res.get("state_workspace"),
            "parsed_inputs": tf_vars, "plan_json": plan_json, "diff": plan["diff"],
            "policy_checks": [], "execution_mode": "destroy", "needs_change": True,
            "approval_status": "pending", "reasoning_cards": reasoning,
            "interrupt_payload": interrupt_payload,
            "confidentiality": {"level": cc.level, "score": cc.score},
            "answer": f"Planned the teardown of **{res['name']}** ({plan['summary']['destroy']} resource(s) "
                      "to destroy, nothing else touched). Review and approve to proceed."}


async def _modify_resource(state: AgentState, config, target: str | None) -> dict:
    """Day-2 MODIFY: change an inventoried resource via its module + variables + approval gate."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    org_id = state["org_id"]

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    await emitter.step(2, f"Resolving “{target or 'resource'}” for modification")
    matches, _kind = await inventory.resolve(org_id, target)
    # Deterministic-first (Prompt 3, mandate 10): a resource NAMED VERBATIM in the message
    # always outranks fuzzy most-recent resolution — a flaked router target must never
    # redirect a mutation to a different resource (found live: "on SpineVM" resolved to the
    # most-recently-created ProbeVM). Two names in one message still ask, never guess.
    named = await inventory.named_in_message(org_id, state.get("message", ""))
    if len(named) == 1 and (not matches or _kind != "name"
                            or matches[0]["name"] != named[0]["name"]):
        if matches and matches[0]["name"] != named[0]["name"]:
            await emitter.step(2, f"Target corrected to “{named[0]['name']}” — named "
                                  "verbatim in your request")
        matches, _kind = [named[0]], "name"
    elif len(named) > 1 and _kind != "name":
        return await _say("Your message names more than one resource "
                          f"({', '.join(n['name'] for n in named)}). Which one should I "
                          "modify? I won't guess on a mutation.")
    if not matches:
        return await _say(f"I couldn't find a resource matching “{target or 'that'}” to modify. "
                          "Tell me its exact name, or create it first — I won't invent one.")
    if len(matches) > 1:
        return await _say(f"More than one resource matches “{target}”: "
                          f"{', '.join(m['name'] for m in matches)}. Which should I modify?")
    res = matches[0]
    template = templates.select(res["cloud"], res["resource_type"])
    caps = _MODIFY_CAPS.get(template.key) if template else None
    if template is None or caps is None:
        supported = ", ".join(sorted(_MODIFY_CAPS))
        return await _say(f"Modifying {res['cloud']}/{res['resource_type']} isn't supported yet. "
                          f"Day-2 modify currently covers: {supported}.")

    changes = await _extract_modification(settings, state["message"])
    # Owner Option A: Azure has no Terraform-native power path — say so honestly, never
    # fall back to an SDK mutation.
    if changes.get("power") and template.key == "azure.vm":
        return await _say(_AZURE_POWER_ANSWER)
    unsupported = sorted(set(changes) - caps)
    if changes and unsupported:
        return await _say(f"On {template.key} I can change: {', '.join(sorted(caps))} — "
                          f"not {', '.join(unsupported)}. Rephrase with a supported change "
                          f"for **{res['name']}**.")
    if not changes:
        hints = {"aws.ec2": "e.g. “stop web-01”, “add inbound port 8501 to web-01”, or “tag env=prod”",
                 "gcp.vm": "e.g. “start web-01” or “add inbound port 8080”",
                 "azure.vm": "e.g. “add inbound ports 8501, 8502”",
                 "aws.s3": "e.g. “turn versioning off”, “expire objects after 30 days”, or “tag env=prod”",
                 "aws.rds": "e.g. “scale to db.t3.large”, “grow storage to 100 GiB”, or “tag env=prod”"}
        return await _say(f"What change should I make to **{res['name']}**? "
                          f"{hints.get(template.key, '')}")

    base = dict(res.get("inputs") or {})
    # OBSERVE BEFORE ACT (Prompt 3, mandate 5): inspect the LIVE security configuration
    # before computing the desired state — never mutate from remembered conversation state.
    # A port the memory says is open but the cloud says is closed (or vice versa) must be
    # decided by the cloud. Best-effort: an unreachable provider falls back to the recorded
    # inputs, stated on the card.
    live_note = None
    if template.key == "aws.ec2" and ("ingress_ports" in changes
                                      or "ingress_ports_remove" in changes):
        live_ports = await _live_ingress_ports(settings, res)
        if live_ports is not None:
            recorded = sorted(int(p) for p in (base.get("ingress_ports") or []))
            if sorted(live_ports) != recorded:
                live_note = (f"live inspection: open ports {sorted(live_ports)} "
                             f"(recorded state said {recorded}) — live cloud wins")
            else:
                live_note = f"live inspection: open ports {sorted(live_ports)} — matches records"
            base["ingress_ports"] = sorted(live_ports)
            await emitter.step(2, f"Inspected live security group · ports {sorted(live_ports)}")
    merged, change_desc = _apply_modification(base, changes)
    # Day-2 pins the RECORDED image (2026-08-17, caught live by the plan guard): the
    # module's AMI data source tracks the latest release, so a mere port change planned an
    # instance REPLACEMENT the day Amazon published a newer AL2023 AMI. A modify never
    # changes the image implicitly — the image recorded at apply time is the image.
    if template.key == "aws.ec2" and not merged.get("ami"):
        recorded_ami = (res.get("attributes") or {}).get("ami_used")
        if recorded_ami:
            merged["ami"] = recorded_ami
    try:
        validated = template.schema(**merged).model_dump()
    except Exception as e:  # noqa: BLE001
        return await _say(f"Couldn't build a valid modification for {res['name']}: {e}")
    change_text = "; ".join(change_desc)

    async def _no_change(detail: str) -> dict:
        """Honest NO_CHANGE terminal: the requested state already holds — nothing to approve,
        nothing to apply, and the run must NEVER claim a change happened (forensic-audit
        remediation, 2026-08-16). Recorded as an immutable `no_change` revision."""
        msg = (f"**Nothing to change on {res['name']}** — {detail} "
               "The live configuration already matches the requested state, so no plan was "
               "sent for approval and nothing was applied.")
        try:
            async with session_scope() as s:
                payload = {"name": res["name"], "cloud": res["cloud"],
                           "region": res.get("region"), "resource_type": res["resource_type"],
                           "inputs": base, "session_id": state.get("session_id"),
                           "run_id": state.get("run_id")}
                await inventory.add_revision(
                    s, org_id, action="no_change", payload=payload,
                    resource_id=uuid.UUID(res["id"]) if res.get("id") else None,
                    actor_user=(state.get("user") or {}).get("username"),
                    reason=state.get("message"), execution_result="no_change")
        except Exception as e:  # noqa: BLE001 - bookkeeping never blocks the honest answer
            log.warning("cloudops.no_change_revision_failed", error=str(e))
        await emitter.step(3, f"No change needed on {res['name']}")
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "no_change", "resolution": msg}}

    # Input-level no-op: the merged desired state equals the recorded state (e.g. closing a
    # port that isn't open). Cheaper and clearer than planning a guaranteed-empty diff.
    if merged == base:
        return await _no_change(change_text.capitalize() + "." if change_text else
                                "the request resolves to the current configuration.")

    await emitter.step(3, f"Planning day-2 change on {res['name']}: {change_text}")
    await timing.start_step(run_id, "cloudops_agent")
    await timing.end_step(run_id, "cloudops_agent", status="done")
    await timing.start_step(run_id, "planner", tool="terraform")
    # Modify runs against the resource's OWN state workspace (Phase 8 / N-08).
    runner = TerraformRunner(res["workspace"] or template.workspace, settings,
                             state_workspace=res.get("state_workspace"), run_id=run_id)

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(validated, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="plan")
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error", retriable=True)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # HARD GUARD: a modify must update in place — a delete/replace here would silently
    # destroy the instance (Phase 8 / N-08).
    violation = plan_guard.check_plan_actions("modify", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="modify", summary=plan["summary"])
        await emitter.step(9, "Safety guard · modify plan would replace/destroy — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        return await _say(violation)

    # ZERO-CHANGE GUARD (forensic-audit remediation, 2026-08-16): a plan of 0/0/0 means the
    # requested change does not exist as far as Terraform is concerned. It must never enter
    # approval and must never be reported as applied — say NO_CHANGE honestly.
    if plan_guard.zero_change(plan["summary"]):
        log.info("cloudops.modify_no_change", run_id=run_id, name=res["name"],
                 requested=change_text)
        return await _no_change(
            f"the Terraform plan is 0 add / 0 change / 0 destroy for “{change_text}”.")

    await timing.start_step(run_id, "policy_evaluation")
    policy_checks = template.policy_fn(validated, runner.planned_resources())  # U1: over the real plan
    policy_checks = policy_checks + cost.checks_for(template.key, validated)  # COST: estimate + guardrail on the card
    await timing.end_step(run_id, "policy_evaluation", status="done")

    plan_json = {"summary": plan["summary"], "diff": plan["diff"],
                 "workspace": res["workspace"] or template.workspace,
                 "state_workspace": res.get("state_workspace"),
                 "policy_checks": policy_checks, "mode": "apply"}
    cc = classify(json.dumps({"modify": res["name"], "changes": change_desc}))
    await emitter.confidentiality(cc.level, cc.score)
    reasoning = [
        {"title": "Day-2 modification", "conf": "",
         "body": f"{change_text.capitalize()} on {res['name']} (resource {res.get('provider_id')})."},
        {"title": "Plan", "conf": "",
         "body": f"+{plan['summary']['add']} ~{plan['summary']['change']} -{plan['summary']['destroy']}"},
    ]
    if live_note:
        reasoning.insert(0, {"title": "Observed live state", "conf": "", "body": live_note})
    await emitter.analysis(summary=f"Planned a day-2 change on {res['name']}; awaiting approval.", cards=reasoning)
    interrupt_payload = {"kind": "approval", "runId": state["run_id"], "workflow": template.key, "plan": plan_json,
                         "diff": plan["diff"], "policyChecks": policy_checks, "mode": "apply",
                         "cloud": res["cloud"], "resource": res["resource_type"]}
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)
    return {"workflow": template.key, "workflow_version": template.version, "cloud": res["cloud"],
            "resource": res["resource_type"], "state_workspace": res.get("state_workspace"),
            "parsed_inputs": validated, "plan_json": plan_json,
            "diff": plan["diff"], "policy_checks": policy_checks, "execution_mode": "apply",
            "needs_change": True, "approval_status": "pending", "reasoning_cards": reasoning,
            "interrupt_payload": interrupt_payload, "confidentiality": {"level": cc.level, "score": cc.score},
            "answer": f"Planned this change on **{res['name']}**: {change_text} "
                      f"(+{plan['summary']['add']} ~{plan['summary']['change']}). "
                      "Review and approve to apply."}


async def cloudops_execute(state: AgentState, config) -> dict:
    """Apply/destroy the approved plan (post-interrupt). Idempotent + streamed to console."""
    emitter = emitter_of(config)
    settings = get_settings()
    if state.get("approval_status") != "approved":
        return {}
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))
    # Use the cloud/resource resolved + persisted by cloudops_plan (never re-default to AWS).
    template = templates.select(state.get("cloud"), state.get("resource"))
    mode = state.get("execution_mode", "apply")
    if template is None:
        await emitter.error("No approved module for the resolved cloud/resource; refusing to execute.",
                            code="template_error")
        return {"outcome": {"status": f"{mode}_failed", "error": "template not found for resolved cloud/resource"}}
    workspace = template.workspace
    # Execute in the SAME per-resource state workspace the plan was made in (Phase 8 / N-08),
    # and with the SAME run_id so the saved plan-file path matches the one plan wrote (A3).
    runner = TerraformRunner(workspace, settings, state_workspace=state.get("state_workspace"),
                             run_id=state.get("run_id"))

    idem_key = idempotency.make_key("tf-exec", state["run_id"], mode)
    if not await idempotency.claim(idem_key):
        # A1: a concurrent apply already holds the claim. NEVER fall through to a second
        # apply on the same state. If it already finished, return its stored result; if it's
        # still in flight, WAIT for the result up to a deadline, then ABORT (409-style) if it
        # still hasn't landed — abort, never execute.
        done = await idempotency.get_result(idem_key) or await idempotency.wait_for_result(idem_key)
        if done:
            return {"outcome": done["result"], "tool_results": [done["result"]]}
        # Prod-hardening (2026-08-17): distinguish a LIVE concurrent apply (heartbeat up →
        # abort, never double) from a DEAD claimant (worker crashed mid-apply → the claim
        # will never resolve). The dead case used to return the misleading "refresh to see
        # the result" abort forever, leaving whatever terraform half-created INVISIBLE.
        # Now it is recorded as an honest, VISIBLE partial (Prompt-1 recovery machinery):
        # destroy/retry can operate on it and the user is told the truth.
        from .supervisor import hb_key as _hb_key
        try:
            from ..cache.redis import get_redis as _get_redis
            claimant_alive = bool(await _get_redis().exists(_hb_key(state["run_id"])))
        except Exception:  # noqa: BLE001 — unknown ⇒ assume alive; abort is the safe answer
            claimant_alive = True
        if claimant_alive:
            log.warning("cloudops.execute_already_in_flight", run_id=state["run_id"], mode=mode)
            return {"outcome": {"status": f"{mode}_aborted",
                                "error": "This change is already being applied by another request; "
                                         "aborting to avoid a duplicate apply."},
                    "answer": "⚠️ This change is already being applied — I stopped here so nothing "
                              "runs twice. Refresh to see the result of the in-flight apply."}
        log.error("cloudops.execute_claim_stale_dead_worker", run_id=state["run_id"], mode=mode)
        try:
            leftover = await runner.state_list()
        except Exception:  # noqa: BLE001 — the state report is best-effort
            leftover = None
        if mode != "destroy":
            await inventory.record_partial(state, template,
                                           "apply interrupted mid-flight (worker died holding "
                                           "the claim)", leftover)
        msg = ("⚠️ A previous attempt to apply this change was interrupted mid-flight (the "
               "worker died). Its Terraform state "
               + (f"holds {len(leftover)} resource(s)" if leftover else "may hold partial resources")
               + " — recorded as PARTIAL so it stays visible. Say “retry” to re-plan against "
                 "the recorded state (safe — Terraform reconciles), or ask me to destroy it.")
        return {"outcome": {"status": f"{mode}_interrupted", "partial": bool(leftover),
                            "partial_resources": (leftover or [])[:20],
                            "error": "prior apply interrupted mid-flight; state requires "
                                     "reconciliation"},
                "answer": msg}

    plan = state.get("plan_json", {}).get("summary", {})
    n = plan.get("destroy", 0) if mode == "destroy" else plan.get("add", 0) + plan.get("change", 0)
    await emitter.step(5, f"{'Destroying' if mode == 'destroy' else 'Applying'} {n} resources…")

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    async def _cg(coro) -> None:
        # Context-graph writes must never fail a successful infra change.
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.cg_write_failed", error=str(e))

    await _cg(cg.add_step(order=3, name=f"terraform_{mode}", agent="cloudops", tool="terraform",
                          status="running", human_vs_auto="auto"))
    try:
        result = await (runner.destroy(state.get("parsed_inputs", {}), on_line) if mode == "destroy"
                        else runner.apply(on_line))
    except TerraformError as e:
        await idempotency.release(idem_key)
        # Classify + explain the failure in the conversation (Phase 7 / BUG-05); raw trace
        # stays in the Logs tab. Also report what (if anything) is left in Terraform state so
        # the user knows whether partial resources remain.
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode=mode)
        try:
            leftover = await runner.state_list()
        except Exception:  # noqa: BLE001 - the state report is best-effort
            leftover = None
        if leftover is not None:
            friendly += ("\n\n**State check:** "
                         + (f"{len(leftover)} resource(s) remain in Terraform state "
                            f"(`{'`, `'.join(leftover[:6])}`) — a retry will reconcile them, or ask "
                            "me to destroy this workspace to clean up."
                            if leftover else "no partial resources were left behind."))
        # U7: one-click retry-with-fix (a genuine new turn through the whole gated flow).
        retry = provider_errors.suggest_retry(
            failure, state.get("message", ""), cloud=state.get("cloud"),
            current_region=(state.get("parsed_inputs") or {}).get("region")
            or (state.get("parsed_inputs") or {}).get("location"))
        await emitter.error(f"terraform {mode} failed: {e}", code="terraform_error",
                            retriable=True, retry=retry)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        await _cg(cg.update_step(order=3, status="failed", error=str(e)))
        # Forensic-audit remediation (2026-08-16): a failed APPLY that left resources in
        # Terraform state used to be INVISIBLE — not in inventory, so it could neither be
        # retried with different inputs (plan guard) nor destroyed ("not something I
        # provisioned") while the real infrastructure kept existing. Record it as a
        # `partial` inventory row + immutable revision so destroy/retry can operate on it.
        if mode != "destroy":
            await inventory.record_partial(state, template, str(e), leftover)
            # Parameter continuity: preserve the validated inputs so a follow-up
            # "retry with t2.micro" doesn't force the user to re-answer everything.
            if state.get("session_id"):
                try:
                    # Preserve in COLLECTION-SPEC shape (the collector's vocabulary), not
                    # tf-var shape — otherwise key pair/CIDR answers are re-asked.
                    spec_vals = params.from_tf_vars(state.get("workflow") or "",
                                                    state.get("parsed_inputs") or {})
                    await params.save_pending(state["session_id"], {
                        "template": state.get("workflow"), "cloud": state.get("cloud"),
                        "resource": state.get("resource"), "action": "create",
                        "collected": {k: v for k, v in spec_vals.items()
                                      if v not in (None, "", [])},
                        "after_failure": True, "context_id": state.get("context_id")})
                except Exception as pe:  # noqa: BLE001 - continuity is best-effort
                    log.warning("cloudops.pending_preserve_failed", error=str(pe))
        return {"outcome": {"status": f"{mode}_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None,
                            "partial": bool(leftover),
                            "partial_resources": (leftover or [])[:20],
                            "retry": retry},
                "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    await idempotency.store_result(idem_key, result)
    await _cg(cg.update_step(order=3, status="done", result={"mode": mode}))
    # D2: the apply already mutated real infrastructure — that cannot be rolled back. So the
    # inventory row and the run outcome are written in ONE transaction, and the outcome carries a
    # self-contained recovery payload (`_inventory`). Result: normally both land together; and
    # even if this write is interrupted, the outcome returned below is persisted by the outer
    # driver WITH the payload, so the orphan sweeper can rebuild a missing inventory row from the
    # run alone. A real applied resource is never invisible.
    actor = (state.get("user") or {}).get("username")
    if mode == "destroy":
        outcome = {"status": "destroyed", **result}
        name = inventory.name_from_inputs(state.get("parsed_inputs") or {}, template.resource)
        try:
            async with session_scope() as s:
                gone = await inventory.mark_destroyed_txn(s, state["org_id"], template.workspace, name)
                for row in gone:
                    # Immutable history: one `destroyed` revision per row, before-state preserved.
                    await inventory.add_revision(
                        s, state["org_id"], action="destroyed",
                        payload={"name": row["name"], "cloud": row["cloud"],
                                 "region": row.get("region"),
                                 "resource_type": row["resource_type"],
                                 "inputs": row.get("inputs"),
                                 "session_id": state.get("session_id"),
                                 "run_id": state.get("run_id")},
                        before={"attributes": row.get("attributes"), "status": row["status"],
                                "provider_id": row.get("provider_id")},
                        resource_id=row["resource_id"], actor_user=actor,
                        reason=state.get("message"), execution_result="destroyed")
                run = await s.get(Run, uuid.UUID(state["run_id"]))
                if run:
                    run.outcome = outcome
        except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a real destroy
            log.warning("cloudops.inventory_failed", error=str(e))
        await inventory.mark_destroyed_graph_only(name, org_id=state["org_id"])
    else:
        payload = inventory.inventory_payload(state, template, result.get("outputs", {}))
        outcome = {"status": "applied", **result, "_inventory": payload}
        graph_action = "created"
        try:
            async with session_scope() as s:
                up = await inventory.upsert_resource(s, state["org_id"], payload)
                graph_action = up["action"]
                # Immutable history in the SAME transaction: a day-2 modify records
                # `modified` with before/after — never another `created`.
                await inventory.add_revision(
                    s, state["org_id"], action=up["action"], payload=payload,
                    before=up["before"], after={"attributes": result.get("outputs", {})},
                    resource_id=up["resource"].id, actor_user=actor,
                    reason=state.get("message"), execution_result="applied")
                run = await s.get(Run, uuid.UUID(state["run_id"]))
                if run:
                    run.outcome = outcome
        except Exception as e:  # noqa: BLE001 - inventory bookkeeping must never fail a real apply
            log.warning("cloudops.inventory_failed", error=str(e))
        await inventory.record_graph(state, template, result.get("outputs", {}),
                                     action=graph_action)
    return {"outcome": outcome, "tool_results": [result]}
