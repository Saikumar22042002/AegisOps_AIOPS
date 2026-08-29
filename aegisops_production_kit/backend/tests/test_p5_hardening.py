"""P5 — credential broker, production preflight, and the DEF-19 parity/cutover decision.

All deterministic (no live cloud, no live model): the broker boundary, the terraform env
dual-path equivalence, the config preflight severities, and the honest cutover gate.
"""

from __future__ import annotations

import pytest

from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── credential broker (P5.3 / ADR-17) ────────────────────────────────────────────────────────

async def test_grant_is_redaction_safe_and_delivers_material_only_via_provider_env():
    from app.security import credential_broker as cb
    s = Settings(aws_access_key_id="AKIA-DEVONLY", aws_secret_access_key="top-secret-shh",
                 aws_default_region="us-east-1")
    g = await cb.resolve(s, org_id=None, provider="aws", env="prod", operation="apply")
    # repr/str NEVER reveal secret material (no leak into logs/traces/events/UI/evidence)
    assert "top-secret-shh" not in repr(g) and "AKIA-DEVONLY" not in repr(g)
    assert "top-secret-shh" not in str(g)
    # the ONE authorized egress carries the real material
    assert g.provider_env()["AWS_SECRET_ACCESS_KEY"] == "top-secret-shh"
    # fingerprint is non-secret and stable
    assert "top-secret-shh" not in g.fingerprint() and g.fingerprint() == g.fingerprint()
    assert g.source == "global_fallback"          # dual-path default until a vault backend


async def test_broker_refuses_when_no_credentials_configured():
    from app.security import credential_broker as cb
    with pytest.raises(cb.CredentialError):
        await cb.resolve(Settings(_env_file=None, aws_access_key_id="", azure_client_id="",
                                  google_cloud_project=""),
                         org_id="o1", provider="aws", env="prod", operation="apply")


async def test_broker_is_pluggable_for_a_future_vault_backend():
    from app.security import credential_broker as cb

    class FakeVault(cb.CredentialBroker):
        async def resolve(self, *, org_id, provider, env, operation):
            return cb.CredentialGrant(org_id=org_id, provider=provider, env=env,
                                      source="broker", scope=f"vault:{org_id}",
                                      _material={"AWS_ACCESS_KEY_ID": "ASIA-short-lived"})
    cb.set_broker(FakeVault())
    try:
        g = await cb.resolve(Settings(), org_id="o1", provider="aws", env="prod",
                             operation="apply")
        assert g.source == "broker" and g.provider_env()["AWS_ACCESS_KEY_ID"].startswith("ASIA")
    finally:
        cb.set_broker(None)


def test_terraform_env_is_byte_identical_across_broker_flag_dual_path():
    """Dual-path: the provider env the Terraform runner builds is identical whether the
    broker flag is off or on (the broker default returns the same global set) — so flipping
    the flag changes WHERE creds come from, never the resulting subprocess env."""
    from app.tools.terraform import TerraformRunner
    kw = dict(aws_access_key_id="AKIA-DEV", aws_secret_access_key="s",
              aws_default_region="us-east-1", google_cloud_project="proj")
    off = TerraformRunner("demo-null", Settings(_env_file=None, aegisops_credential_broker="off", **kw))
    on = TerraformRunner("demo-null", Settings(_env_file=None, aegisops_credential_broker="on", **kw))
    e_off = off._env(include_ws=False, plugin_cache=False)
    e_on = on._env(include_ws=False, plugin_cache=False)
    cred_keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
                 "GOOGLE_PROJECT")
    assert {k: e_off.get(k) for k in cred_keys} == {k: e_on.get(k) for k in cred_keys}
    assert e_on["AWS_ACCESS_KEY_ID"] == "AKIA-DEV"


def test_terraform_env_uses_a_brokered_grant_override_when_set():
    from app.security.credential_broker import CredentialGrant
    from app.tools.terraform import TerraformRunner
    r = TerraformRunner("demo-null", Settings(_env_file=None, aegisops_credential_broker="on",
                        aws_access_key_id="AKIA-GLOBAL", aws_secret_access_key="g"))
    r.set_credential_grant(CredentialGrant(org_id="o1", provider="aws", env="prod",
                                           source="broker", scope="vault:o1",
                                           _material={"AWS_ACCESS_KEY_ID": "ASIA-PERORG"}))
    env = r._env(include_ws=False, plugin_cache=False)
    assert env["AWS_ACCESS_KEY_ID"] == "ASIA-PERORG"   # per-org grant overrides the global set


# ── production preflight (P5 hardening) ──────────────────────────────────────────────────────

def test_preflight_local_is_lenient_warnings_not_blocks():
    from app import preflight
    rpt = preflight.run(Settings(app_env="local", aegisops_event_bus="memory"))
    assert not rpt.blocked                            # local keeps booting
    assert rpt.app_env == "local"


def test_preflight_blocks_unsafe_production_posture():
    from app import preflight
    # non-local + memory bus + no metrics token → blocks
    rpt = preflight.run(Settings(app_env="production", aegisops_event_bus="memory",
                                 aegisops_metrics_token=""))
    assert rpt.blocked
    checks = {f.check: f.severity for f in rpt.findings}
    assert checks["event_bus"] == "block" and checks["metrics_auth"] == "block"


def test_preflight_flags_global_creds_without_broker_offlocal():
    from app import preflight
    # Prompt 4: the posture must otherwise be production-safe — the hardened preflight now
    # blocks shipped-default secrets, so give it real ones to isolate the broker warn.
    rpt = preflight.run(Settings(app_env="production", aegisops_event_bus="redis",
                                 aegisops_metrics_token="t", aws_access_key_id="AKIA-x",
                                 aegisops_credential_broker="off",
                                 secret_key="unit-test-long-random-value-0123456789abcdef",
                                 keycloak_admin_password="unit-test-not-default"))
    cred = next(f for f in rpt.findings if f.check == "credential_broker")
    assert cred.severity == "warn" and "broker" in cred.detail
    assert not rpt.blocked                            # a warn, not a hard block


def test_preflight_never_permits_autonomous_mode():
    from app import preflight
    rpt = preflight.run(Settings(app_env="production", aegisops_event_bus="redis",
                                 aegisops_metrics_token="t",
                                 aegisops_permission_mode="AUTONOMOUS"))
    assert any(f.check == "permission_mode" and f.severity == "block" for f in rpt.findings)


def test_preflight_blocks_shipped_defaults_offlocal():
    """Prompt 4: default SECRET_KEY, default Keycloak admin password, and wildcard CORS are
    startup-blocking off-local; the same posture only warns in local dev."""
    from app import preflight
    bad = Settings(app_env="production", aegisops_event_bus="redis",
                   aegisops_metrics_token="t", cors_origins="*")  # defaults left in place
    rpt = preflight.run(bad)
    sev = {f.check: f.severity for f in rpt.findings}
    assert sev["secret_key"] == "block"
    assert sev["keycloak_admin_password"] == "block"
    assert sev["cors_origins"] == "block"
    assert rpt.blocked
    local = preflight.run(Settings(app_env="local", cors_origins="*"))
    assert not local.blocked                          # local keeps booting (warn posture)


def test_preflight_accepts_hardened_production_posture():
    from app import preflight
    rpt = preflight.run(Settings(
        app_env="production", aegisops_event_bus="redis", aegisops_metrics_token="t",
        secret_key="unit-test-long-random-value-0123456789abcdef",
        keycloak_admin_password="unit-test-not-default",
        cors_origins="https://ops.example.com"))
    assert not rpt.blocked


# ── DEF-19 parity + cutover decision ─────────────────────────────────────────────────────────

def test_parity_report_covers_deterministic_dims_and_defers_live_ones_honestly():
    from app.evals import parity
    rpt = parity.evaluate(Settings(gemini_api_key="k"))
    verdicts = {r.dimension: r.verdict for r in rpt.results}
    # deterministic dimensions are actually checked and pass
    assert verdicts["read_only_boundary"] == "PASS"
    assert verdicts["mutation_governed"] == "PASS"
    assert verdicts["objective_interpretation_deterministic"] == "PASS"
    # live dimensions are DEFERRED (dead key) — never faked as PASS
    assert verdicts["behavioral_eval_gate_both_topologies"] == "DEFERRED"
    assert rpt.deferred, "live parity dimensions must be present and deferred"


def test_cutover_decision_stays_dark_when_live_parity_unproven():
    from app.evals import parity
    rpt = parity.evaluate(Settings(gemini_api_key="k"))
    decision = parity.decide_cutover(rpt)
    assert decision.may_cutover is False           # the correct, compliant outcome
    assert "DEFERRED" in decision.reason or "dark" in decision.reason


def test_cutover_would_permit_only_with_zero_fails_and_zero_deferred():
    from app.evals import parity
    clean = parity.ParityReport()
    clean.add("x", "PASS", "ok")
    assert parity.decide_cutover(clean).may_cutover is True   # the gate logic is honest
