"""Secret redaction for logs, SSE streams, console output, and context-graph writes.

Best-effort masking of common secret shapes. Defense-in-depth: secrets should never be
logged in the first place, but this guarantees streamed CLI/console output is masked.
"""

from __future__ import annotations

import re

_MASK = "••••REDACTED••••"

# Order matters; most specific first. Each pattern falls into exactly one substitution branch
# by its capturing-group count (see `redact`): 3 groups = keep-first-and-last (mask the middle);
# 2 groups = keep-first, mask-second; 0/1 groups = mask the whole match.
_PATTERNS: list[re.Pattern[str]] = [
    # Private-key block: keep the BEGIN/END markers, mask the body (3 groups).
    re.compile(r"(?i)(-----BEGIN[^-]+PRIVATE KEY-----)(.*?)(-----END[^-]+PRIVATE KEY-----)", re.DOTALL),
    # AWS access key ids: long-term (AKIA) + STS temporary/sandbox (ASIA) (0 groups → whole).
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Authorization: Bearer <token> (2 groups → keep the header prefix, mask the token).
    re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._\-]+)"),
    # key=value / "key": "value" for any secret-shaped key — including compound/underscored/
    # quoted names (aws_session_token, AWS_SESSION_TOKEN, "SessionToken", AccessKeyId,
    # client_secret). 2 groups: group1 = name+separator (+opening quote, kept), group2 = the
    # secret value (masked). This masks the VALUE — the previous 3-group form leaked it.
    re.compile(
        r"(?i)([\w.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|session[_-]?token|private[_-]?key|credential)[\w.\-]*[\"']?\s*[=:]\s*[\"']?)"
        r"([^\s\"';,]+)"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        groups = pat.groups
        if groups >= 3:
            out = pat.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(m.lastindex)}" if m.group(1) else _MASK, out)
        elif groups == 2:
            out = pat.sub(lambda m: f"{m.group(1)}{_MASK}", out)
        else:
            out = pat.sub(_MASK, out)
    return out


_SENSITIVE_KEYS = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|"
    r"session[_-]?token|credential|private[_-]?key)")


def redact_dict(data: dict) -> dict:
    """Recursively mask values whose key looks sensitive; redact string values otherwise."""
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact_dict(i) if isinstance(i, dict) else (redact(i) if isinstance(i, str) else i) for i in v]
        elif isinstance(v, str):
            out[k] = _MASK if _SENSITIVE_KEYS.search(k) else redact(v)
        else:
            out[k] = v
    return out
