"""STAB P2-3 — continuation turns log honestly, never "intent classified: None (None)".

Live (screenshot 5, 2026-07-13): a params-reply turn's Logs tab read
"intent classified: None (None)" / "routed -> None agent" — a continuation carries no
fresh classification, and the log must say that instead of printing None placeholders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.artifacts import _classification_log_lines

_TS = datetime(2026, 7, 20, 9, 0, 0, tzinfo=timezone.utc)


def test_classified_runs_keep_the_two_familiar_lines():
    run = SimpleNamespace(intent="create_aws_instance", confidence=0.95,
                          domain="cloudops", created_at=_TS)
    msgs = [line["msg"] for line in _classification_log_lines(run)]
    assert msgs == ["intent classified: create_aws_instance (0.95)", "routed -> cloudops agent"]


def test_continuation_runs_say_so_instead_of_none_none():
    run = SimpleNamespace(intent=None, confidence=None, domain="cloudops", created_at=_TS)
    msgs = [line["msg"] for line in _classification_log_lines(run)]
    assert len(msgs) == 1
    assert "continuation turn" in msgs[0] and "classification skipped" in msgs[0]
    assert "None" not in msgs[0]


def test_continuation_without_a_domain_stays_honest():
    run = SimpleNamespace(intent=None, confidence=None, domain=None, created_at=_TS)
    msgs = [line["msg"] for line in _classification_log_lines(run)]
    assert "None" not in msgs[0]
