"""BUGFIX-1 — gcp.cloudsql engine normalization (live acceptance run 2, 2026-07-13).

The live plan card showed `Approved engine (PostgreSQL) = false, detail='postgres'`: the
param extractor had passed the user's own word ("postgres") straight into
`database_version`, which the schema accepted un-normalized — failing the policy check AND
(had it been approved) the provider's enum at apply. The schema now canonicalizes honest
spellings at the single validation choke point every plan path goes through.

Also pinned here: the OTHER half of the live card — `No world-open authorized networks =
false, detail='0.0.0.0/0'` — is NOT a bug. It is MS-9/B2 by design: the schema keeps the
legacy world-open default so pre-enhancement inputs re-plan identically, and the policy
check fails VISIBLY so the approver sees exactly that.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.templates import _gcp_cloudsql_policy
from app.schemas.workflows import GCPCloudSQLInputs


def _check(checks: list[dict], name: str) -> dict:
    return next(c for c in checks if c["name"] == name)


# ── the exact live failure, fixed ──────────────────────────────────────────────────────────

def test_live_shape_bare_postgres_normalizes_and_passes_policy():
    # live extraction: "create a cloud sql postgres database named accept-sql" + db-f1-micro
    i = GCPCloudSQLInputs(name="accept-sql", tier="db-f1-micro", database_version="postgres")
    assert i.database_version == "POSTGRES_15"
    ck = _check(_gcp_cloudsql_policy(i.model_dump()), "Approved engine (PostgreSQL)")
    assert ck["passed"] is True
    assert ck["detail"] == "POSTGRES_15"


@pytest.mark.parametrize("raw,canonical", [
    ("PostgreSQL", "POSTGRES_15"),          # bare engine, mixed case
    ("postgresql 16", "POSTGRES_16"),       # engine + version, space-separated
    ("postgres-14", "POSTGRES_14"),         # hyphenated
    ("mysql", "MYSQL_8_0"),                 # bare mysql → default version
    ("sqlserver 2019 standard", "SQLSERVER_2019_STANDARD"),
])
def test_honest_spellings_canonicalize(raw, canonical):
    assert GCPCloudSQLInputs(name="db", database_version=raw).database_version == canonical


# ── B1/B2: canonical inputs pass through unchanged; the default is unchanged ───────────────

def test_canonical_values_unchanged_and_default_kept():
    assert GCPCloudSQLInputs(name="db").database_version == "POSTGRES_15"          # B2 default
    assert (GCPCloudSQLInputs(name="db", database_version="POSTGRES_15")
            .database_version == "POSTGRES_15")                                    # B1 passthrough
    assert (GCPCloudSQLInputs(name="db", database_version="MYSQL_8_0")
            .database_version == "MYSQL_8_0")


def test_valid_non_postgres_engine_fails_the_check_visibly():
    # normalization must NOT hide the org policy: a valid MySQL input stays MySQL and the
    # approved-engine check fails VISIBLY on the card — the approver decides, never a guess.
    i = GCPCloudSQLInputs(name="db", database_version="MYSQL_8_0")
    ck = _check(_gcp_cloudsql_policy(i.model_dump()), "Approved engine (PostgreSQL)")
    assert ck["passed"] is False
    assert ck["detail"] == "MYSQL_8_0"


def test_unmappable_engine_rejected_with_examples_never_guessed():
    with pytest.raises(ValidationError, match="POSTGRES_15"):
        GCPCloudSQLInputs(name="db", database_version="oracle")


# ── the by-design half of the live card, pinned so it is never "fixed" into silence ────────

def test_world_open_legacy_default_fails_visibly_by_design():
    i = GCPCloudSQLInputs(name="db")                       # B2 legacy default: ["0.0.0.0/0"]
    ck = _check(_gcp_cloudsql_policy(i.model_dump()), "No world-open authorized networks")
    assert ck["passed"] is False and "0.0.0.0/0" in ck["detail"]


def test_scoped_networks_pass_and_private_path_switches_check():
    scoped = GCPCloudSQLInputs(name="db", authorized_networks=["10.8.0.0/16"])
    ck = _check(_gcp_cloudsql_policy(scoped.model_dump()), "No world-open authorized networks")
    assert ck["passed"] is True
    private = GCPCloudSQLInputs(name="db", private_network="projects/p/global/networks/n")
    names = [c["name"] for c in _gcp_cloudsql_policy(private.model_dump())]
    assert "Network exposure" in names and "No world-open authorized networks" not in names
