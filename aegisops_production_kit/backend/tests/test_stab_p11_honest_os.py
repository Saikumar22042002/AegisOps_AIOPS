"""STAB P1-1 — never silently substitute a requested-but-unsupported value.

Live failures (Screenshots 7-8 and 4, 2026-07-13): "create a windows vm in gcp" planned
LINUX without a word — the extractor normalized windows→windows-2022, GCPComputeInputs had
no os validator, and the module's image lookup fell back to its default. And the reply
`mybucket-sai@22042792002` was extracted as the DIFFERENT valid name
`mybucket-sai-22042792002` and planned (owner-ordered explicit audit case).

The honest behavior: an unsupported OS is a REFUSAL naming where the request IS supported
(COMP-c family, extended to CREATE); a name the user didn't literally type is never used.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents import cloudops, templates
from app.schemas.workflows import AWSEC2Inputs, AzureVMInputs, GCPComputeInputs
from app.settings import Settings


# ── the schema-level refusal (rides the existing per-field re-ask in cloudops) ──

def test_gcp_vm_refuses_windows_naming_where_it_is_supported():
    with pytest.raises(ValidationError) as ei:
        GCPComputeInputs(name="harsha-vm-01", os="windows-2022")
    msg = str(ei.value)
    assert "Linux-only" in msg
    assert "aws.ec2" in msg and "azure.vm" in msg


def test_gcp_vm_accepts_every_real_linux_choice():
    for os_ in ("debian-12", "ubuntu-22.04", "ubuntu-24.04"):
        assert GCPComputeInputs(name="x", os=os_).os == os_


def test_windows_stays_first_class_on_the_clouds_that_have_it():
    assert AzureVMInputs(name="x", os="windows-2022").os == "windows-2022"
    assert AWSEC2Inputs(name="x", os="windows-2022").os == "windows-2022"


def test_the_exact_screenshot_shape_fails_validation_not_terraform():
    """The live utterance's extracted shape must die at the schema with the honest message,
    never reach terraform as a silently-substituted Linux plan."""
    with pytest.raises(ValidationError) as ei:
        GCPComputeInputs(name="harsha-vm-01", machine_type="e2-micro",
                         os="windows-2022", allowed_cidr="103.120.51.5/32")
    assert "Windows Server is available" in str(ei.value)


# ── the verbatim-name guard in extraction (the s3 rewrite audit case) ──

class _FakeGemini:
    enabled = True


def _extract(monkeypatch, template_key: str, message: str, llm_result: dict) -> dict:
    import asyncio

    s = Settings(_env_file=None)
    monkeypatch.setattr(cloudops, "get_gemini", lambda _s: _FakeGemini())

    async def fake_classify(_s, _system, _msg):
        return dict(llm_result)

    monkeypatch.setattr(cloudops.llm, "classify_json", fake_classify)
    tpl = templates.by_key(template_key)
    return asyncio.run(cloudops._extract_inputs(s, tpl, message))


def test_a_rewritten_bucket_name_is_dropped_never_planned(monkeypatch):
    out = _extract(monkeypatch, "aws.s3", "mybucket-sai@22042792002",
                   {"bucket_name": "mybucket-sai-22042792002"})
    assert "bucket_name" not in out, \
        "the model invented a different name — it must be re-asked, never used"


def test_a_verbatim_name_is_kept(monkeypatch):
    out = _extract(monkeypatch, "aws.s3", "call it bucket-2792002-9640211061",
                   {"bucket_name": "bucket-2792002-9640211061"})
    assert out["bucket_name"] == "bucket-2792002-9640211061"


def test_choice_normalization_stays_exempt(monkeypatch):
    """os synonym mapping (windows→windows-2022) is DESIRED — allowlist validators guard
    those fields; the verbatim rule applies to identity fields only."""
    out = _extract(monkeypatch, "azure.vm", "harsha-vm-01, windows, Standard_D2s_v5",
                   {"name": "harsha-vm-01", "os": "windows-2022", "size": "Standard_D2s_v5"})
    assert out["os"] == "windows-2022"
    assert out["name"] == "harsha-vm-01"
