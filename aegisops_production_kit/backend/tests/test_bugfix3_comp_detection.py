"""BUGFIX-3 — COMP false-positive on database noun phrases (live acceptance run 2,
2026-07-13).

The live transcript: "create a postgres cloudsql instance named accept-sql, smallest tier"
was compound-intercepted («That's a **compound request** for 2 independent resources (the
VM, the database)») — `_detected_categories` counted the word "instance" as compute even
though "Cloud SQL instance" is the DATABASE product's own name. The qualifier now collapses
onto its database head before detection; genuine compounds (a free-standing instance, a db
AND another resource) are still caught.
"""

from __future__ import annotations

from app.agents.cloudops import _comp_intercept, _detected_categories


def _msg(message, action="create", cloud=None, target=None):
    return {"message": message, "action": action, "cloud": cloud, "target": target}


# ── the exact live utterance falls through as ONE resource ─────────────────────────────────

def test_live_utterance_is_not_compound():
    assert _comp_intercept(
        _msg("create a postgres cloudsql instance named accept-sql, smallest tier")) is None


def test_db_qualified_instance_variants_are_single_resources():
    assert _detected_categories("a postgres cloudsql instance") == ["database"]
    assert _detected_categories("an rds instance named orders-db") == ["database"]
    assert _detected_categories("a mysql server") == ["database"]
    assert _detected_categories("a sql server instance") == ["database"]
    assert _detected_categories("a cloud sql instance") == ["database"]
    assert _comp_intercept(_msg("create an rds instance named orders-db")) is None


# ── genuine compounds stay caught (the fix must not swallow them) ──────────────────────────

def test_free_standing_instance_is_still_compute():
    assert set(_detected_categories("an instance and a bucket")) == {"compute", "storage"}
    out = _comp_intercept(_msg("create an instance and an s3 bucket"))
    assert out is not None and "compound" in out["answer"].lower()


def test_database_plus_other_resource_is_still_compound():
    out = _comp_intercept(_msg("create an rds instance and an s3 bucket"))
    assert out is not None and "compound" in out["answer"].lower()
    out2 = _comp_intercept(_msg("create a vm and a postgres database"))
    assert out2 is not None and "compound" in out2["answer"].lower()


def test_ec2_instance_still_means_compute():
    # compute's own qualifier is untouched — "ec2 instance" keeps meaning compute
    assert _detected_categories("an ec2 instance") == ["compute"]
