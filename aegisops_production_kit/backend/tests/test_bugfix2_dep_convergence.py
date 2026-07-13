"""BUGFIX-2 — DEP ask convergence (live acceptance run 2, 2026-07-13).

The live transcript: "create a small t3.micro ec2 instance inside a new vpc" → params
collected over two turns → the DEP parent-VPC ask fired («Which vpc should this use? You
have 2: "accept-web3-net" (…), "acc-web-net" (…). Or say "new" to create one.») — and then
EVERY reply ("new", "accept-web3-net") produced the identical ask again: the reply was
re-resolved from scratch, `_WANTS_NEW` only matches the in-sentence form "new vpc", and
nothing mapped the reply onto the slot. The asking turn now persists `dep_ask`
(parent_type + the real candidates) alongside the pending params record, and the next
turn's reply is mapped back onto exactly that slot — "new" forces the create-first DAG,
a candidate's name narrows to that parent's REAL recorded facts. An unrecognized reply
maps to nothing and the ask honestly repeats (never a guess).
"""

from __future__ import annotations

from app.agents import intent_guard
from app.agents.dependency import choice_from_reply, resolve_closure

# the ask exactly as the live turn persisted it
_LIVE_ASK = {"parent_type": "vpc",
             "options": [{"name": "accept-web3-net", "provider_id": "vpc-097f59d6a03ba385f"},
                         {"name": "acc-web-net", "provider_id": "vpc-0ec2a52084f7d7b65"}]}

# org inventory mirroring the live account: two recorded VPCs, one with recorded outputs
_ACTIVE = [
    {"cloud": "aws", "resource_type": "vpc", "name": "accept-web3-net",
     "provider_id": "vpc-097f59d6a03ba385f",
     "attributes": {"public_subnet_ids": ["subnet-aaa", "subnet-bbb"]}},
    {"cloud": "aws", "resource_type": "vpc", "name": "acc-web-net",
     "provider_id": "vpc-0ec2a52084f7d7b65", "attributes": {}},
]

_EC2_INPUTS = {"name": "accept-ec2", "instance_type": "t3.micro"}


# ── the reply mapper: the exact live utterances ────────────────────────────────────────────

def test_live_reply_new_maps_to_create_first():
    assert choice_from_reply("new", _LIVE_ASK) == {"parent_type": "vpc", "choice": "__new__"}


def test_live_reply_candidate_name_maps_to_that_candidate():
    assert choice_from_reply("accept-web3-net", _LIVE_ASK) == {
        "parent_type": "vpc", "choice": "accept-web3-net"}


def test_reply_variants_map_honestly():
    assert choice_from_reply("New.", _LIVE_ASK)["choice"] == "__new__"
    assert choice_from_reply("a new one", _LIVE_ASK)["choice"] == "__new__"
    assert choice_from_reply("use acc-web-net please", _LIVE_ASK)["choice"] == "acc-web-net"
    # provider_id works too — the mapper returns the candidate's NAME for the resolver
    assert choice_from_reply("vpc-0ec2a52084f7d7b65", _LIVE_ASK)["choice"] == "acc-web-net"


def test_unrecognized_reply_maps_to_nothing_never_guessed():
    assert choice_from_reply("something else entirely", _LIVE_ASK) is None
    assert choice_from_reply("new", None) is None          # no pending ask → no mapping
    assert choice_from_reply("", _LIVE_ASK) is None


# ── the resolver honors the mapped choice on exactly the asked slot ────────────────────────

def test_choice_new_forces_the_create_first_dag():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE,
                        message="new", dep_choice={"parent_type": "vpc", "choice": "__new__"})
    assert c.status == "dag"
    assert [s["template_key"] for s in c.dag] == ["aws.vpc", "aws.ec2"]
    assert c.dag[0]["inputs"]["name"] == "accept-ec2-net"      # parent named after the child


def test_choice_candidate_fills_from_its_real_recorded_facts():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE, message="accept-web3-net",
                        dep_choice={"parent_type": "vpc", "choice": "accept-web3-net"})
    assert c.status == "complete"
    assert c.inputs["subnet_id"] == "subnet-aaa"               # attr:public_subnet_ids[0]
    assert any("accept-web3-net" in n for n in c.notes)        # provenance for the card


def test_choice_candidate_without_recorded_facts_asks_again_honestly():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE, message="acc-web-net",
                        dep_choice={"parent_type": "vpc", "choice": "acc-web-net"})
    assert c.status == "ask"                                   # facts missing → ask, never guess
    assert "recorded facts don't include" in c.question


def test_choice_for_a_different_slot_changes_nothing():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE, message="new",
                        dep_choice={"parent_type": "resource_group", "choice": "__new__"})
    assert c.status == "ask"                                   # the vpc ask repeats untouched


# ── the OLD broken shape, pinned: without the mapping the ask loops ────────────────────────

def test_without_dep_choice_a_bare_new_still_asks_thats_the_bug_shape():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE, message="new", dep_choice=None)
    assert c.status == "ask"


def test_ask_persists_what_the_next_turn_needs():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE, message="")
    assert c.status == "ask" and c.parent_type == "vpc"
    assert {o["name"] for o in c.options} == {"accept-web3-net", "acc-web-net"}


# ── single-shot phrasing keeps working (how the flagship passed live) ──────────────────────

def test_single_shot_new_vpc_still_goes_straight_to_dag():
    c = resolve_closure("aws.ec2", dict(_EC2_INPUTS), _ACTIVE,
                        message="create a small t3.micro ec2 instance inside a new vpc")
    assert c.status == "dag"


# ── router continuation: the replies must reach cloudops as answers, never re-classify ─────

def test_router_treats_the_live_replies_as_answers():
    assert intent_guard.is_new_request("new") is False
    assert intent_guard.is_new_request("accept-web3-net") is False
    assert intent_guard.is_new_request("vpc-0ec2a52084f7d7b65") is False
    # while the opener remains a fresh request
    assert intent_guard.message_shape(
        "create a small t3.micro ec2 instance inside a new vpc") == "request"
