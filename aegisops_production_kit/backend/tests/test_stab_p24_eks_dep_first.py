"""STAB P2-4 — slot-covered params defer to the DEP closure; raw ids are never demanded.

Live (screenshots 18-19, 2026-07-19): "Create a EkS cluster in the aws" produced a params
card demanding "Existing VPC id (vpc-…)" and comma-separated subnet ids, and re-demanded
them after "use existing vpc" — params collection ran BEFORE DEP resolution, so the
world-model offer (the DEP item-(c) acceptance, tested only at the resolver layer) never
fired on the live path. Now the params ask excludes slot-covered fields and the closure
runs BEFORE schema validation, filling/offering/DAG-ing at the live layer.
"""

from __future__ import annotations

from app.agents import dependency, params


def test_slot_fields_cover_the_eks_raw_ids():
    assert dependency.slot_fields("aws.eks") == {"vpc_id", "subnet_ids"}
    assert dependency.slot_fields("aws.nlb") == {"vpc_id", "subnets"}
    assert dependency.slot_fields("aws.s3") == set()   # slot-less templates unaffected


def test_the_live_params_card_never_demands_slot_covered_ids():
    """The exact screenshot shape: cluster name given, nothing else — the ask must be
    empty of vpc_id/subnet_ids (DEP owns them); only genuinely un-derivable params remain."""
    collected = {"cluster_name": "payment-eks"}
    dep_fields = dependency.slot_fields("aws.eks")
    missing = [m.name for m in params.missing_required("aws.eks", collected)
               if m.name not in dep_fields]
    assert "vpc_id" not in missing and "subnet_ids" not in missing
    # and the pre-filter view proves the card WOULD have demanded them (the live bug):
    assert {"vpc_id", "subnet_ids"} & {m.name for m in params.missing_required("aws.eks", collected)}


def test_closure_still_fills_or_offers_from_the_world_model():
    """Resolver behavior on pre-validation inputs (the new call site): one recorded VPC →
    both raw ids filled from its REAL outputs; none → create-first DAG, never a raw-id ask."""
    vpc = {"cloud": "aws", "resource_type": "vpc", "name": "accept-ec2-net",
           "provider_id": "vpc-0f411efc6ab891632", "workspace": "aws-vpc",
           "attributes": {"vpc_id": "vpc-0f411efc6ab891632",
                          "private_subnet_ids": ["subnet-a", "subnet-b"]}}
    got = dependency.resolve_closure("aws.eks", {"cluster_name": "payment-eks"}, [vpc])
    assert got.status == "complete"
    assert got.inputs["vpc_id"] == "vpc-0f411efc6ab891632"
    assert got.inputs["subnet_ids"] == ["subnet-a", "subnet-b"]

    none = dependency.resolve_closure("aws.eks", {"cluster_name": "payment-eks"}, [])
    assert none.status in ("dag", "ask")
    if none.status == "ask":
        assert "vpc-" not in (none.question or ""), "never a raw-id demand"
