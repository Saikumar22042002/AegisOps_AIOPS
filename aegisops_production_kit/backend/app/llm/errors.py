"""Canonical model-error taxonomy (Redesign/04 §4, 05 §11).

One exception type + a `kind` enum, instead of a class per provider failure: callers
branch on `kind`/`retriable`, adapters map SDK exceptions into it. `context_overflow`
triggers compact-and-retry, never failover (06 §7); `auth_permanent` opens the breaker
for the binding. `provider_detail` is redacted at construction — never a raw secret.
"""

from __future__ import annotations

import re
from typing import Literal

ErrorKind = Literal[
    "rate_limited",            # our side throttled (retry with backoff / Retry-After)
    "upstream_rate_limited",   # provider capacity (429/529 family — failover candidate)
    "context_overflow",        # prompt too large — compact-and-retry, NEVER failover
    "auth",                    # transient auth (expired token) — retry once, then breaker
    "auth_permanent",          # bad/revoked key or missing SDK/config — breaker + failover
    "content_filtered",        # provider safety block on the output
    "refusal",                 # model declined the task
    "timeout",                 # request deadline exceeded
    "unavailable",             # 5xx / transport / connection failures
    "invalid_request",         # our request was malformed — never retry as-is
]

# Kinds the executor may retry on the SAME binding (P1.6).
RETRIABLE: frozenset[str] = frozenset({
    "rate_limited", "upstream_rate_limited", "timeout", "unavailable", "auth",
})
# Kinds that make the executor fail over to the NEXT binding in the RoutePlan.
FAILOVER: frozenset[str] = frozenset({
    "upstream_rate_limited", "timeout", "unavailable", "auth_permanent",
})

_SECRET_RE = re.compile(r"(?i)(key|token|secret|authorization)[=:\s]+\S+")


class ModelError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, provider: str = "",
                 retry_after_s: float | None = None) -> None:
        self.kind: ErrorKind = kind
        self.provider = provider
        self.retry_after_s = retry_after_s
        self.provider_detail = _SECRET_RE.sub(r"\1=[redacted]", message)[:500]
        super().__init__(f"{kind}: {self.provider_detail}")

    @property
    def retriable(self) -> bool:
        return self.kind in RETRIABLE

    @property
    def failover(self) -> bool:
        return self.kind in FAILOVER

    def payload(self) -> dict[str, str]:
        """The StreamEvent(error=…) / ToolResult.error shape."""
        return {"kind": self.kind, "message": self.provider_detail}


def classify_status(status: int | None, message: str, *, provider: str = "",
                    retry_after_s: float | None = None) -> ModelError:
    """Map an HTTP-ish provider status to the taxonomy (adapters may refine per SDK)."""
    kind: ErrorKind
    if status in (429, 529):
        kind = "upstream_rate_limited"
    elif status == 401:
        kind = "auth_permanent"   # providers rarely distinguish; 401 is usually a bad key
    elif status == 403:
        kind = "auth"
    elif status in (408, 504):
        kind = "timeout"
    elif status is not None and status >= 500:
        kind = "unavailable"
    elif status in (400, 404, 422):
        kind = "invalid_request"
    else:
        kind = "unavailable"
    return ModelError(kind, message, provider=provider, retry_after_s=retry_after_s)
