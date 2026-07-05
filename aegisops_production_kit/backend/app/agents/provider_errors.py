"""Provider-failure classification (Phase 7 / BUG-05).

Terraform surfaces cloud-provider rejections as raw stack traces. Those stay in the Logs tab
(useful evidence), but the CONVERSATION must explain the failure in plain English: what failed,
the likely cause, and the exact next step. This module maps common provider-error signatures to
that explanation — deterministically, from the error text alone (no LLM, no guessing).

Everything here is honest triage of a REAL failure — it never retries, masks, or fakes success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ProviderFailure:
    kind: str        # machine-readable failure class
    title: str       # one-line headline
    cause: str       # likely cause, plain English
    next_step: str   # the exact action that unblocks it
    url: str | None = None  # provider console link when one is embedded in the error


_ACTIVATION_URL = re.compile(r"https://console\.developers\.google\.com/apis/[^\s\"'\\]+")


def classify_provider_error(text: str) -> ProviderFailure | None:
    """Map a raw provider/terraform error to a classified failure, or None if unrecognized."""
    t = text or ""
    low = t.lower()

    # ── Expired / invalid credentials (sandbox creds last ~1h) ──
    if re.search(r"expiredtoken|security token included in the request is expired|"
                 r"invalid_grant|aadsts7000222|aadsts700016|invalid client secret|"
                 r"request had invalid authentication credentials|could not load credentials|"
                 r"no valid credential sources|invalidclienttokenid", low):
        return ProviderFailure(
            kind="credentials_expired",
            title="Cloud credentials are expired or invalid",
            cause="The credentials the platform is using were rejected by the provider — sandbox "
                  "credentials are short-lived (about an hour).",
            next_step="Refresh the credentials in `.env` (and the GCP service-account key if "
                      "applicable), restart the API, then retry this request.",
        )

    # ── GCP: required API not enabled on the project ──
    if "service_disabled" in low or "accessnotconfigured" in low or "has not been used in project" in low:
        m = _ACTIVATION_URL.search(t)
        api = "Compute Engine API" if "compute" in low else "required Google Cloud API"
        return ProviderFailure(
            kind="api_disabled",
            title=f"The {api} is not enabled on this GCP project",
            cause="Google rejected the request because the project has never enabled that API — "
                  "this is a project setting, not a fault in the plan.",
            next_step=(f"Enable it at {m.group(0)} , wait a few minutes for it to propagate, then "
                       "retry." if m else
                       "Enable the API in the Google Cloud console (APIs & Services → Enable), "
                       "wait a few minutes, then retry."),
            url=m.group(0) if m else None,
        )

    # ── Azure: service principal lacks a role assignment ──
    if "authorizationfailed" in low or "does not have authorization to perform action" in low:
        return ProviderFailure(
            kind="iam_denied",
            title="The Azure service principal doesn't have permission for this action",
            cause="Azure returned 403 AuthorizationFailed — the service principal has no role "
                  "assignment that allows creating/modifying these resources on the subscription.",
            next_step="Grant the service principal the **Contributor** role on the subscription "
                      "(or target resource group) in Azure IAM, then retry.",
        )

    # ── AWS: IAM denial ──
    if re.search(r"unauthorizedoperation|accessdenied(?:exception)?|not authorized to perform", low):
        return ProviderFailure(
            kind="iam_denied",
            title="The AWS credentials don't have permission for this action",
            cause="AWS rejected the call with an authorization error — the IAM identity behind "
                  "the configured credentials lacks the required permission.",
            next_step="Attach the missing IAM permission (the raw log names the denied action) "
                      "or use credentials with the right role, then retry.",
        )

    # ── Name already taken (global namespaces: S3, storage accounts, Cloud SQL, …) ──
    if re.search(r"bucketalreadyexists|bucketalreadyownedbyyou|storageaccountalreadytaken|"
                 r"storageaccountalreadyexists|409[^\n]*already ?exists|already ?exists[^\n]*409|"
                 r"entityalreadyexists|alreadyexists", low):
        return ProviderFailure(
            kind="name_taken",
            title="That name is already taken",
            cause="The provider rejected the create because the name is already in use — bucket "
                  "and storage-account names are globally unique across ALL customers, so common "
                  "names like `my-bucket` are long gone.",
            next_step="Pick a more distinctive, globally-unique name (e.g. prefix it with your "
                      "org or project) and ask me again.",
        )

    # ── Quota / limits ──
    if re.search(r"quota ?exceeded|limitexceeded|limit exceeded|exceeded quota|too many", low):
        return ProviderFailure(
            kind="quota_exceeded",
            title="A provider quota or limit was hit",
            cause="The account/project has reached a service quota for this resource type.",
            next_step="Request a quota increase in the provider console, free up existing "
                      "resources, or choose a smaller size/count, then retry.",
        )

    # ── Invalid region / zone / location ──
    if re.search(r"invalid (?:value for field '?zone|region|location)|location is not accepting|"
                 r"unknown zone|invalidparametervalue[^\n]*(?:zone|region)|"
                 r"not (?:available|found) in (?:zone|region|location)", low):
        return ProviderFailure(
            kind="bad_location",
            title="The requested region/zone can't serve this request",
            cause="The provider rejected the location — it may not exist, may not offer this "
                  "resource type/size, or may be closed to new deployments.",
            next_step="Retry with a different region/zone (name it in your request), or a "
                      "size that's available there.",
        )

    return None


def failure_message(f: ProviderFailure | None, raw_error: str, mode: str = "apply") -> str:
    """Compose the plain-English conversation message for a failed plan/apply/destroy."""
    if f is None:
        head = f"**The {mode} failed.** The provider rejected the operation"
        tail = (f":\n\n> {raw_error.strip()[:400]}\n\nThe full output is in the **Logs** tab. "
                "Nothing was billed for resources that weren't created — say the word and I'll retry "
                "once the underlying issue is fixed.")
        return head + tail
    lines = [f"**The {mode} failed — {f.title}.**",
             "",
             f"**Why:** {f.cause}",
             f"**Next step:** {f.next_step}",
             "",
             "The raw provider output is in the **Logs** tab. Nothing was changed beyond what the "
             "log shows; retry after the fix and I'll pick it up from a clean plan."]
    return "\n".join(lines)
