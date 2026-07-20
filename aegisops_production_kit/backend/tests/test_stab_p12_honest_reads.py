"""STAB P1-2 — structured, honest read answers.

Live failures (screenshots 9/17, 2026-07-13/14):
(a) the inventory listing rendered as ONE dense paragraph — “• ”-prefixed lines joined by
    single newlines, which markdown collapses;
(b) "Did any instances are running in aws ?" was classified action=read +
    intent=`sync_resources` — a label that slipped the question guard's side-effect rewrite
    (sync_ wasn't in the prefix list) and carried a state-mutation narrative;
(c) rows whose cloud's live discovery just failed still read as plain "active".
"""

from __future__ import annotations

from app.agents import intent_guard
from app.agents.cloudops import _render_inventory_list

_ROWS = [
    {"name": "sai-test", "resource_type": "ec2", "cloud": "aws", "region": "us-east-1",
     "provider_id": "i-0abeec2cee664b4f5", "created_at": "2026-07-05T10:20:00", "status": "active"},
    {"name": "gcp-vm-test", "resource_type": "vm", "cloud": "gcp", "region": "us-central1",
     "provider_id": "691874278998008988", "created_at": "2026-07-05T13:22:00", "status": "active"},
]


# ── (a) real markdown table, never a collapsed paragraph ──

def test_listing_is_a_real_markdown_table():
    out = _render_inventory_list(list(_ROWS))
    assert "| Name | Type | Cloud | Region | Created | Id | Status |" in out
    assert "| **sai-test** | ec2 | aws | us-east-1 |" in out
    assert "•" not in out, "the old bullet glyph collapses into a paragraph in markdown"
    header_idx = out.index("| Name |")
    assert out[:header_idx].endswith("\n\n" ), "a blank line must precede the table or GFM won't parse it"


# ── (b) a question never wears a sync/write-shaped intent ──

def test_the_exact_screenshot_question_sheds_sync_resources():
    msg = "Did any instances are running in aws ?"
    assert intent_guard.is_question(msg)
    out = intent_guard.guard_classification(
        msg, {"domain": "cloudops", "action": "read", "intent": "sync_resources",
              "resource": "instances"})
    assert out is not None, "sync_resources on a question MUST be rewritten"
    assert out["intent"].startswith("query_")
    assert out["action"] == "read"


def test_genuine_read_intents_pass_untouched():
    out = intent_guard.guard_classification(
        "how many vms are running in gcp?",
        {"domain": "cloudops", "action": "read", "intent": "query_instances",
         "resource": "instances"})
    assert out is None


# ── (c) discovery-failure rows say so, never plain "active" ──

def test_rows_on_a_failed_cloud_read_unverified():
    out = _render_inventory_list(list(_ROWS), unverified_clouds={"gcp"})
    assert "| **gcp-vm-test** | vm | gcp | us-central1 " in out
    gcp_row = next(line for line in out.splitlines() if "gcp-vm-test" in line)
    assert "unverified — live discovery failed" in gcp_row
    aws_row = next(line for line in out.splitlines() if "sai-test" in line)
    assert "unverified" not in aws_row and "| active |" in aws_row
