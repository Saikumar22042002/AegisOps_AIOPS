"""STAB P1-5 — attaching a DATABASE to an instance gets the honest decomposition.

Live (screenshot 21, 2026-07-19): «Create a mysql db in the aws and attach the Sai-test-v1
which we created earlier» — the attach half was silently dropped; only the DB was planned
(and then died on the identifier, P1-4). COMP's attach honesty covered storage mounts only.
A managed database is *connected to*, never attached: create the DB → day-2 scope its
allowed source to the instance → the app uses the connection string. Half a request is
never silently discarded.
"""

from __future__ import annotations

from app.agents.cloudops import _comp_intercept


def _state(message: str, action: str = "create") -> dict:
    return {"message": message, "action": action}


def test_the_exact_live_utterance_is_answered_honestly_not_half_dropped():
    out = _comp_intercept(_state(
        "Create a mysql db in the aws and attach the Sai-test-v1 which we created earlier"))
    assert out is not None
    ans = out["answer"]
    assert "connected to" in ans and "day-2" in ans
    assert out.get("needs_clarification") is True   # the user decides — nothing is planned yet
    assert "connection string" in ans


def test_db_connected_to_a_network_never_gets_the_instance_attach_answer():
    # "…and connect it to my vpc" lands in the pre-existing compound clarify (db+network
    # categories) — fine; what it must NEVER get is the instance-attach decomposition.
    out = _comp_intercept(_state("create a postgres db and connect it to my vpc"))
    assert out is None or "connected to, not attached" not in out["answer"].replace("*", "")
    # A DEP-linked placement phrasing falls through untouched (the DEP/network paths own it).
    assert _comp_intercept(_state(
        "create a mysql database attached to the payments subnet")) is None


def test_storage_mount_branch_is_untouched():
    out = _comp_intercept(_state("create a gcs bucket and mount it on my vm"))
    assert out is not None
    assert "gcsfuse" in out["answer"]


def test_a_plain_db_create_is_never_intercepted():
    assert _comp_intercept(_state("create a mysql db in aws named payments-db")) is None
