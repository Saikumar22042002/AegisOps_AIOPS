"""Unit tests for the confidentiality classifier."""

from __future__ import annotations

from app.security.confidentiality import classify


def test_low_for_plain_text() -> None:
    c = classify("List the regions available in this account.")
    assert c.level == "Low"
    assert c.score < 0.3


def test_high_for_secrets() -> None:
    c = classify("aws_secret_access_key = AKIAIOSFODNN7EXAMPLE and password=hunter2")
    assert c.level in {"Medium", "High"}
    assert c.score >= 0.3


def test_private_key_is_high() -> None:
    c = classify("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    assert c.level == "High"


def test_empty() -> None:
    c = classify("")
    assert c.level == "Low" and c.score == 0.0
