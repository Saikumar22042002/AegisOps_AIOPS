"""Cloud read-only SDK import guards (6.3 regression guard).

The CloudOps availability precheck downgrades a cloud to "not configured" when its SDK fails to
import — which would silently mislabel a cloud that actually HAS credentials. This guards that the
pinned SDKs import in the runtime environment (e.g. azure-mgmt-resource ≥23 relocated
ResourceManagementClient to azure.mgmt.resource.resources).
"""

from __future__ import annotations


def test_azure_sdk_imports_cleanly():
    from app.tools.azure import _HAVE_AZURE
    assert _HAVE_AZURE is True, "Azure discovery SDK failed to import — availability checks would lie"


def test_cloud_tool_factories_present():
    from app.tools import aws, azure, gcp
    assert hasattr(aws, "get_aws")
    assert hasattr(azure, "get_azure")
    assert hasattr(gcp, "get_gcp")


def test_availability_reports_configured_when_creds_present():
    # With creds in the environment the reader must report enabled (not the "not configured" path).
    # Gated on the raw env so it stays honest (and skips per-cloud) on a bare checkout.
    import os

    from app.settings import get_settings
    from app.tools import aws, azure, gcp

    s = get_settings()
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
        assert aws.get_aws(s).enabled
    if os.getenv("AZURE_CLIENT_SECRET") and os.getenv("AZURE_SUBSCRIPTION_ID"):
        assert azure.get_azure(s).enabled
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") and os.getenv("GOOGLE_CLOUD_PROJECT"):
        assert gcp.get_gcp(s).enabled
