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

    async def list_instances(self, region: str | None = None) -> list[dict[str, Any]]:
        """All EC2 instances in the region with state/type/name (read-only)."""
        ec2 = self._client("ec2", region)
        res = await self._run(ec2.describe_instances)
        out: list[dict[str, Any]] = []
        for resv in res.get("Reservations", []):
            for i in resv.get("Instances", []):
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)
                out.append({"id": i["InstanceId"], "state": i.get("State", {}).get("Name"),
                            "type": i.get("InstanceType"), "name": name,
                            "public_ip": i.get("PublicIpAddress"), "private_ip": i.get("PrivateIpAddress")})
        return out

    async def list_buckets(self) -> list[dict[str, Any]]:
        """All S3 buckets in the account (bucket listing is global, read-only)."""
        s3 = self._client("s3")
        res = await self._run(s3.list_buckets)
        return [{"name": b["Name"],
                 "created": b["CreationDate"].isoformat() if b.get("CreationDate") else None}
                for b in res.get("Buckets", [])]

    async def bucket_taken(self, name: str) -> bool | None:
        """Is this globally-unique bucket name already in use (by anyone)? Read-only HeadBucket.

        True = taken (ours or another account's — 403 means it exists but isn't ours),
        False = free, None = undetermined (treat as unknown; let the plan/apply decide).
        """
        s3 = self._client("s3")
        try:
            await self._run(s3.head_bucket, Bucket=name)
            return True
        except Exception as e:  # noqa: BLE001 - botocore ClientError carries the status code
            code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) if hasattr(e, "response") else ""
            if code in {"404", "NoSuchBucket", "NotFound"}:
                return False
            if code in {"403", "AccessDenied", "Forbidden"}:
                return True  # exists, owned by someone else
            log.warning("aws.head_bucket_failed", bucket=name, error=str(e))
            return None

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
