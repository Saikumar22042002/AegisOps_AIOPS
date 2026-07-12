"""DEP — dependency closure resolution, strict order: named → world model → stated default →
create-first DAG. Acceptance: (a) VPC→EC2 DAG; (b) RG→storage DAG; (c) EKS offered existing
VPCs; (d) two VPCs → asks. Pure resolver tests — `active` is the org inventory shape
`inventory.list_active` returns.
"""

from __future__ import annotations

from app.agents.dependency import resolve_closure


def _vpc(name, pid, subnets=True):
    attrs = {"vpc_id": pid}
    if subnets:
        attrs |= {"private_subnet_ids": [f"{pid}-priv-a", f"{pid}-priv-b"],
                  "public_subnet_ids": [f"{pid}-pub-a"]}
    return {"name": name, "cloud": "aws", "resource_type": "vpc", "provider_id": pid,
            "attributes": attrs}


def _rg(name):
    return {"name": name, "cloud": "azure", "resource_type": "resource_group",
            "provider_id": f"/subscriptions/x/resourceGroups/{name}", "attributes": {}}


# ── (a) VPC→EC2 create-first DAG ───────────────────────────────────────────────────────────

def test_a_new_vpc_request_yields_vpc_then_ec2_dag():
    c = resolve_closure("aws.ec2", {"name": "web", "region": "us-east-1"}, active=[],
                        message="create an ec2 named web in a new vpc")
    assert c.status == "dag"
    assert [s["template_key"] for s in c.dag] == ["aws.vpc", "aws.ec2"]  # parents FIRST
    parent, child = c.dag
    assert parent["inputs"]["name"] == "web-net" and parent["inputs"]["region"] == "us-east-1"
    assert parent["provides"] == "vpc"
    assert child["wires"] == {"subnet_id": "public_subnet_ids[0]"}  # wired to REAL outputs
    assert child["depends_on"] == "aws.vpc"


def test_a_ec2_without_new_and_no_vpcs_takes_the_stated_default_not_a_dag():
    c = resolve_closure("aws.ec2", {"name": "web"}, active=[], message="create an ec2 named web")
    assert c.status == "complete"
    assert any("default VPC" in n for n in c.notes)  # stated, never silent


# ── (b) RG→storage create-first DAG ────────────────────────────────────────────────────────

def test_b_storage_without_rg_yields_rg_then_storage_dag():
    c = resolve_closure("azure.storage", {"account_name": "acmelogs01", "location": "eastus"},
                        active=[], message="create a storage account acmelogs01")
    assert c.status == "dag"
    assert [s["template_key"] for s in c.dag] == ["azure.resource_group", "azure.storage"]
    parent, child = c.dag
    assert parent["inputs"]["name"] == "acmelogs01-rg"
    assert parent["inputs"]["location"] == "eastus"
    assert child["wires"] == {"resource_group": "input:name"}  # RG addressed by its name


def test_b_storage_with_one_existing_rg_uses_it():
    c = resolve_closure("azure.storage", {"account_name": "acmelogs01"},
                        active=[_rg("rg-payments")], message="")
    assert c.status == "complete"
    assert c.inputs["resource_group"] == "rg-payments"
    assert any("rg-payments" in n and "world model" in n for n in c.notes)


# ── (c) EKS offered existing VPCs ──────────────────────────────────────────────────────────

def test_c_eks_single_existing_vpc_fills_vpc_and_subnets_from_real_facts():
    c = resolve_closure("aws.eks", {"cluster_name": "pay"}, active=[_vpc("net-1", "vpc-100")],
                        message="create an eks cluster pay")
    assert c.status == "complete"
    assert c.inputs["vpc_id"] == "vpc-100"
    assert c.inputs["subnet_ids"] == ["vpc-100-priv-a", "vpc-100-priv-b"]  # recorded outputs
    assert any("net-1" in n for n in c.notes)


def test_c_eks_two_existing_vpcs_are_offered():
    c = resolve_closure("aws.eks", {"cluster_name": "pay"},
                        active=[_vpc("net-1", "vpc-100"), _vpc("net-2", "vpc-200")], message="")
    assert c.status == "ask"
    assert {o["provider_id"] for o in c.options} == {"vpc-100", "vpc-200"}
    assert "net-1" in c.question and "net-2" in c.question and "new" in c.question


def test_c_eks_vpc_without_recorded_subnets_asks_never_guesses():
    c = resolve_closure("aws.eks", {"cluster_name": "pay"},
                        active=[_vpc("net-bare", "vpc-300", subnets=False)], message="")
    assert c.status == "ask"
    assert "subnet_ids" in c.question  # names what it honestly can't derive


# ── (d) two VPCs → asks (EC2 too — never guess a placement) ────────────────────────────────

def test_d_ec2_with_two_vpcs_asks_instead_of_defaulting():
    c = resolve_closure("aws.ec2", {"name": "web"},
                        active=[_vpc("net-1", "vpc-100"), _vpc("net-2", "vpc-200")], message="")
    assert c.status == "ask"
    assert len(c.options) == 2


def test_d_ec2_with_one_vpc_uses_it_over_the_default():
    # World model outranks the stated default in the strict order.
    c = resolve_closure("aws.ec2", {"name": "web"}, active=[_vpc("net-1", "vpc-100")], message="")
    assert c.status == "complete"
    assert c.inputs["subnet_id"] == "vpc-100-pub-a"  # a REAL recorded subnet, never the vpc id
    assert any("world model" in n for n in c.notes)


# ── named always wins ─────────────────────────────────────────────────────────────────────

def test_named_value_wins_over_everything():
    c = resolve_closure("aws.eks", {"cluster_name": "pay", "vpc_id": "vpc-named",
                                    "subnet_ids": ["s-1", "s-2"]},
                        active=[_vpc("net-1", "vpc-100"), _vpc("net-2", "vpc-200")], message="")
    assert c.status == "complete"
    assert c.inputs["vpc_id"] == "vpc-named"          # untouched
    assert c.inputs["subnet_ids"] == ["s-1", "s-2"]   # untouched
    assert any("as you named it" in n for n in c.notes)


def test_templates_without_slots_pass_through():
    c = resolve_closure("aws.s3", {"bucket_name": "b1"}, active=[], message="")
    assert c.status == "complete" and c.inputs == {"bucket_name": "b1"} and c.notes == []
