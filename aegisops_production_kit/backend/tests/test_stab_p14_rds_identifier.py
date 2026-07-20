"""STAB P1-4 — RDS identifier normalized at the schema, never validated by Terraform.

Live (screenshot 21, 2026-07-19): «Create a mysql db in the aws and attach the Sai-test-v1»
put `Sai-test-v1` straight into `terraform plan`, which died with the raw provider error
(`only lowercase alphanumeric characters and hyphens allowed in "identifier"`). The exact
BUGFIX-1 class: gcp.cloudsql's engine got a schema normalizer, aws.rds's identifier didn't.
Honest spellings canonicalize (case); junk is refused with the rule + an example, which the
cloudops per-field re-ask renders as a clarification.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.workflows import AWSRDSInputs


def test_the_exact_live_shape_canonicalizes_instead_of_dying_at_plan():
    assert AWSRDSInputs(identifier="Sai-test-v1").identifier == "sai-test-v1"


def test_canonical_identifiers_pass_through_unchanged():
    assert AWSRDSInputs(identifier="payments-db").identifier == "payments-db"  # B1: no rename


@pytest.mark.parametrize("bad", ["1db", "-db", "db-", "d--b", "", "db_x", "x" * 64])
def test_junk_is_refused_with_the_rule_and_an_example(bad):
    with pytest.raises(ValidationError) as ei:
        AWSRDSInputs(identifier=bad)
    msg = str(ei.value)
    assert "payments-db" in msg or "identifier" in msg


def test_whitespace_is_trimmed_not_rejected():
    assert AWSRDSInputs(identifier="  payments-db ").identifier == "payments-db"
