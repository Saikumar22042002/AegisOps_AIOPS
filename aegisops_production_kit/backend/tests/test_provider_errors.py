"""Provider-failure classification (Phase 7 / BUG-05) — every error signature from the manual
test screenshots maps to a friendly, actionable explanation; unknown errors still get an honest
generic message (never a bare stack trace as the only user-visible outcome)."""

from __future__ import annotations

import pytest

from app.agents.provider_errors import classify_provider_error, failure_message

# Real signatures observed in screenshots 9 (Azure 403) and 12 (GCP SERVICE_DISABLED), plus the
# S3 409 and the recurring sandbox-credential expiry.
_AZURE_403 = ('Error: checking for presence of existing resource group: unexpected status 403 '
              '(403 Forbidden) with error: AuthorizationFailed: The client with object id '
              'does not have authorization to perform action '
              "'Microsoft.Resources/subscriptions/resourcegroups/read'")
_GCP_DISABLED = ('Error 403: Compute Engine API has not been used in project user-cbjomebtnwwf '
                 'before or it is disabled. Enable it by visiting '
                 'https://console.developers.google.com/apis/api/compute.googleapis.com/overview?project=user-cbjomebtnwwf '
                 'then retry. reason: "SERVICE_DISABLED", accessNotConfigured')
_S3_409 = ('Error: creating S3 Bucket (my-bucket): BucketAlreadyExists: The requested bucket '
           'name is not available. Status Code: 409')
_AWS_EXPIRED = ('error configuring Terraform AWS Provider: ExpiredToken: The security token '
                'included in the request is expired')
_AWS_IAM = ('UnauthorizedOperation: You are not authorized to perform this operation: '
            'ec2:RunInstances')
_QUOTA = 'Error: Error creating instance: googleapi: Error 403: Quota exceeded for quota metric'
_BAD_ZONE = "Error: Invalid value for field 'zone': 'us-central9-z'. Unknown zone."


@pytest.mark.parametrize("raw,kind", [
    (_AZURE_403, "iam_denied"),
    (_GCP_DISABLED, "api_disabled"),
    (_S3_409, "name_taken"),
    (_AWS_EXPIRED, "credentials_expired"),
    (_AWS_IAM, "iam_denied"),
    (_QUOTA, "quota_exceeded"),
    (_BAD_ZONE, "bad_location"),
])
def test_signatures_classified(raw, kind):
    f = classify_provider_error(raw)
    assert f is not None, f"should classify: {raw[:60]}"
    assert f.kind == kind
    assert f.title and f.cause and f.next_step  # complete, actionable explanation


def test_gcp_activation_url_extracted():
    f = classify_provider_error(_GCP_DISABLED)
    assert f.url and f.url.startswith("https://console.developers.google.com/apis/")
    assert f.url in failure_message(f, _GCP_DISABLED, mode="apply")


def test_azure_403_next_step_names_contributor():
    f = classify_provider_error(_AZURE_403)
    assert "Contributor" in f.next_step


def test_s3_409_next_step_asks_for_unique_name():
    f = classify_provider_error(_S3_409)
    assert "unique" in f.next_step.lower()


def test_expired_creds_point_at_env_refresh():
    f = classify_provider_error(_AWS_EXPIRED)
    assert ".env" in f.next_step


def test_unknown_error_still_gets_honest_message():
    raw = "Error: something completely novel exploded"
    assert classify_provider_error(raw) is None
    msg = failure_message(None, raw, mode="apply")
    assert "failed" in msg.lower() and "Logs" in msg
    assert "novel exploded" in msg  # the raw reason is quoted, not hidden


def test_message_mentions_logs_tab_and_mode():
    f = classify_provider_error(_AZURE_403)
    msg = failure_message(f, _AZURE_403, mode="apply")
    assert msg.startswith("**The apply failed")
    assert "Logs" in msg and "Next step:" in msg
