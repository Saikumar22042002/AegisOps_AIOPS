"""COMP — honest compound / attach / OS-change handling (Phase-3 exit item). The chatbot
must never silently pick one resource from a compound ask, pretend to mount storage it
can't configure in-guest, or fake an in-place OS swap. Crucially, a DEPENDENCY-driven ask
("an EC2 inside a new VPC", DLV-12) is NOT a compound-refuse — the DEP DAG still owns it.
"""

from __future__ import annotations

from app.agents.cloudops import _comp_intercept, _detected_categories


def _msg(message, action="create", cloud=None, target=None):
    return {"message": message, "action": action, "cloud": cloud, "target": target}


# ── (a) compound independent resources → one-at-a-time offer, never silent pick ────────────

def test_compound_two_independent_resources_offers_one_at_a_time():
    out = _comp_intercept(_msg("create an ubuntu vm and a gcs bucket"))
    assert out is not None and out.get("needs_clarification") is True
    a = out["answer"].lower()
    assert "compound" in a and "one at a time" in a
    assert "vm" in a and "bucket" in a           # both named, neither dropped
    assert "start with" in a                     # proceeds with the first on confirmation


def test_compound_names_a_sensible_order():
    out = _comp_intercept(_msg("create a load balancer and a vm and a database"))
    assert out is not None and "then" in out["answer"].lower()


# ── the DEP boundary: dependency-linked asks must fall through (NOT compound) ──────────────

def test_dependency_ask_is_not_compound_dlv12():
    # DLV-12 flagship — "and ... inside it" is a DEP DAG, the exec loop owns it.
    assert _comp_intercept(_msg("create a VPC and an EC2 inside it")) is None
    assert _comp_intercept(_msg("create an ec2 in a new vpc")) is None
    assert _comp_intercept(_msg("create a vm in my prod-network")) is None


def test_single_resource_falls_through():
    assert _comp_intercept(_msg("create an ubuntu vm named web")) is None
    assert _comp_intercept(_msg("create a gcs bucket named logs")) is None


# ── (b) attach / mount → in-guest honesty + the fuse hint, never a fake attach ─────────────

def test_attach_storage_is_honest_about_in_guest_mounting():
    out = _comp_intercept(_msg("create a gcs bucket and attach it to my vm"))
    assert out is not None
    a = out["answer"]
    assert "in-guest" in a.lower() and "can't" in a.lower()
    assert "gcsfuse" in a and "s3fs" in a and "blobfuse" in a.lower() or "blobfuse2" in a
    assert "create the storage" in a.lower() or "create the bucket" in a.lower()


def test_bare_mount_request_also_honest():
    out = _comp_intercept(_msg("mount an s3 bucket on the web server", action="modify"))
    assert out is not None and "in-guest" in out["answer"].lower()


# ── (c) OS-change on an existing VM → guarded refuse + destroy-recreate path ───────────────

def test_os_change_refuses_in_place_and_offers_recreate():
    out = _comp_intercept(_msg("change the OS of web-01 to windows", action="modify",
                               target="web-01"))
    assert out is not None
    a = out["answer"].lower()
    assert "can't change the operating system" in a and "in place" in a
    assert "destroy" in a and "create a fresh vm" in a
    assert "web-01" in out["answer"]


def test_os_change_on_gcp_notes_windows_lives_elsewhere():
    out = _comp_intercept(_msg("switch my gcp vm gce-box to windows", action="modify",
                               cloud="gcp", target="gce-box"))
    assert out is not None
    assert "azure.vm" in out["answer"] and "aws.ec2" in out["answer"]


def test_os_change_is_not_triggered_by_a_port_modify():
    # a normal day-2 modify must NOT be captured by the OS-change interceptor
    assert _comp_intercept(_msg("add inbound port 8080 to web-01", action="modify",
                                target="web-01")) is None


# ── category detection sanity ──────────────────────────────────────────────────────────────

def test_category_detection():
    assert set(_detected_categories("an ubuntu vm and a gcs bucket")) == {"compute", "storage"}
    assert _detected_categories("a postgres database") == ["database"]
    # BUGFIX-3 (live run 2): a database's own noun phrase is ONE resource — the "instance"/
    # "server" qualifier never counts as compute ("Cloud SQL instance" is the product name).
    assert _detected_categories("a postgres cloudsql instance") == ["database"]
    assert _detected_categories("an rds instance") == ["database"]
    assert _detected_categories("a sql server instance") == ["database"]
    # …while a free-standing instance still means compute (real compounds stay caught)
    assert set(_detected_categories("an instance and a bucket")) == {"compute", "storage"}


def test_router_prompt_keeps_these_off_destroy():
    """The router must never misroute compound/attach/OS-change to destroy — the prompt
    reserves destroy for explicit destroy/delete/remove/terminate and names OS-change +
    power as modify. (Pins the guardrail language; the live LLM classification is DLV-era.)"""
    from pathlib import Path
    from app.agents import router
    src = Path(router.__file__).read_text(encoding="utf-8")
    assert "destroy/delete/remove/terminate" in src
    assert "CHANGE/SWITCH the OS" in src and "never destroy" in src
