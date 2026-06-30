"""AWS read-only discovery / availability / verification (boto3). Never provisions."""

from __future__ import annotations

from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    import boto3
    from botocore.config import Config as BotoConfig

    _HAVE_BOTO = True
except Exception:  # noqa: BLE001
    _HAVE_BOTO = False


class AWSError(Exception):
    pass


class AWSReader:
    def __init__(self, settings: Settings) -> None:
        self.region = settings.aws_default_region
        self.enabled = bool(_HAVE_BOTO and settings.aws_access_key_id and settings.aws_secret_access_key)
        self._kwargs = {
            "aws_access_key_id": settings.aws_access_key_id or None,
            "aws_secret_access_key": settings.aws_secret_access_key or None,
            "aws_session_token": settings.aws_session_token or None,
            "region_name": self.region,
        }

    def _client(self, service: str, region: str | None = None):
        if not self.enabled:
            raise AWSError("AWS credentials are not configured")
        kwargs = dict(self._kwargs)
        if region:
            kwargs["region_name"] = region
        return boto3.client(service, config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}), **kwargs)

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    async def list_vpcs(self, region: str | None = None) -> list[dict[str, Any]]:
        ec2 = self._client("ec2", region)
        res = await self._run(ec2.describe_vpcs)
        out = []
        for v in res["Vpcs"]:
            name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), None)
            out.append({"id": v["VpcId"], "cidr": v.get("CidrBlock"), "is_default": v.get("IsDefault"), "name": name, "state": v.get("State")})
        return out

    async def list_subnets(self, vpc_id: str, region: str | None = None) -> list[dict[str, Any]]:
        ec2 = self._client("ec2", region)
        res = await self._run(ec2.describe_subnets, Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        return [{"id": s["SubnetId"], "az": s["AvailabilityZone"], "cidr": s["CidrBlock"], "public": s.get("MapPublicIpOnLaunch")} for s in res["Subnets"]]

    async def list_eks_clusters(self, region: str | None = None) -> list[str]:
        eks = self._client("eks", region)
        res = await self._run(eks.list_clusters)
        return res.get("clusters", [])

    async def describe_eks_cluster(self, name: str, region: str | None = None) -> dict[str, Any]:
        eks = self._client("eks", region)
        res = await self._run(eks.describe_cluster, name=name)
        c = res["cluster"]
        return {"name": c["name"], "status": c["status"], "version": c.get("version"), "endpoint": c.get("endpoint"), "arn": c.get("arn")}

    async def list_databases(self, region: str | None = None) -> list[dict[str, Any]]:
        rds = self._client("rds", region)
        res = await self._run(rds.describe_db_instances)
        return [{"id": d["DBInstanceIdentifier"], "engine": d["Engine"], "status": d["DBInstanceStatus"], "class": d.get("DBInstanceClass")} for d in res["DBInstances"]]

    async def check_quota_eks(self, region: str | None = None) -> dict[str, Any]:
        """Availability pre-check: current EKS cluster count vs a typical soft limit."""
        clusters = await self.list_eks_clusters(region)
        return {"current_clusters": len(clusters), "headroom": max(0, 100 - len(clusters))}

    async def ping(self) -> bool:
        sts = self._client("sts")
        ident = await self._run(sts.get_caller_identity)
        return "Account" in ident


_reader: AWSReader | None = None


def get_aws(settings: Settings) -> AWSReader:
    global _reader
    if _reader is None:
        _reader = AWSReader(settings)
    return _reader
