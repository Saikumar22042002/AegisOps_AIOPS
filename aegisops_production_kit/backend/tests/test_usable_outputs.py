"""Phase-A USABLE OUTPUTS (03_TEST_MATRIX §F — guards N-02, N-06).

Screenshot 1/2: VM applied with `ingress_ports = []` → SSH timed out; key was CLI-only.
Screenshot 3: S3 applied but no summary in chat.

Target APIs (Phase B):
  • `allowed_cidr` collected for VM modules and flowing to the module (opens 22/3389 to it);
  • `app.agents.cards.success_card(resource_type, outputs, inputs) -> str | None`;
  • one-time credential reveal endpoint POST /runs/{run_id}/credentials (RBAC'd, one-shot).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents import params


# ═══ N-02 — CIDR-scoped ingress is collected and reaches Terraform ═══════════════════════════

class TestAllowedCidr:
    def test_vm_modules_collect_allowed_cidr(self):
        for key in ("aws.ec2", "azure.vm", "gcp.vm"):
            names = {p.name for p in params.specs_for(key)}
            assert "allowed_cidr" in names, f"{key} must collect an allowed source CIDR"

    def test_allowed_cidr_is_decision_critical(self):
        # Default-closed is only safe if the USER decided it — so it must be asked.
        missing = {p.name for p in params.missing_required("aws.ec2", {})}
        assert "allowed_cidr" in missing

    def test_allowed_cidr_validation(self):
        from app.schemas.workflows import AWSEC2Inputs
        ok = AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04",
                          allowed_cidr="203.0.113.7/32")
        assert ok.allowed_cidr == "203.0.113.7/32"
        with pytest.raises(Exception):
            AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04",
                         allowed_cidr="not-a-cidr")
        # "none"/"skip" ⇒ closed (empty), an explicit user decision.
        closed = AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04",
                              allowed_cidr="none")
        assert closed.allowed_cidr == ""


# ═══ N-06 — resource-appropriate success cards ═══════════════════════════════════════════════

class TestSuccessCards:
    def test_vm_card_has_connectivity(self):
        from app.agents.cards import success_card
        card = success_card("ec2", {"public_dns": "ec2-1-2-3-4.compute-1.amazonaws.com",
                                    "public_ip": "1.2.3.4", "login_user": "ubuntu",
                                    "key_name": "sai-test-key", "instance_id": "i-0abc"},
                            {"allowed_cidr": "203.0.113.7/32"})
        assert card and "ssh" in card and "ubuntu@" in card and "i-0abc" in card

    def test_s3_card_has_identifiers(self):
        from app.agents.cards import success_card
        card = success_card("s3", {"bucket_name": "sai2792002-bucket",
                                   "bucket_arn": "arn:aws:s3:::sai2792002-bucket",
                                   "region": "us-east-1"}, {})
        assert card
        for frag in ("sai2792002-bucket", "arn:aws:s3:::sai2792002-bucket", "us-east-1"):
            assert frag in card
        assert "s3.console.aws.amazon.com" in card or "console" in card.lower()

    def test_vpc_card_has_id_and_cidr(self):
        from app.agents.cards import success_card
        card = success_card("vpc", {"vpc_id": "vpc-0abc"}, {"cidr_block": "10.0.0.0/16"})
        assert card and "vpc-0abc" in card and "10.0.0.0/16" in card

    def test_db_card_has_endpoint_not_password(self):
        from app.agents.cards import success_card
        card = success_card("rds", {"endpoint": "db.xyz.us-east-1.rds.amazonaws.com:5432"}, {})
        assert card and "db.xyz.us-east-1.rds.amazonaws.com" in card
        assert "password" not in card.lower() or "secrets manager" in card.lower()

    def test_unknown_type_still_gets_generic_card(self):
        from app.agents.cards import success_card
        card = success_card("gcs", {"bucket_name": "b", "self_link": "https://x"}, {})
        assert card and "b" in card


# ═══ N-02 — one-time credential reveal (endpoint contract; RBAC; one-shot) ═══════════════════

class TestCredentialReveal:
    def test_reveal_requires_auth(self, client: TestClient):
        r = client.post("/runs/00000000-0000-0000-0000-000000000000/credentials",
                        json={"output": "private_key_pem"})
        assert r.status_code == 401

    async def test_reveal_is_one_shot(self, live_redis, monkeypatch):
        """Contract: the same credential can be fetched exactly once; the second attempt is
        refused (410) and the value never persists anywhere."""
        from app.api import artifacts as artifacts_api
        claim = getattr(artifacts_api, "_claim_reveal", None)
        assert claim is not None, "one-time reveal claim helper missing"
        run_id = "itest-reveal-run"
        assert await claim(run_id, "private_key_pem") is True
        assert await claim(run_id, "private_key_pem") is False   # second reveal refused
        await live_redis.delete(f"reveal:{run_id}:private_key_pem")
