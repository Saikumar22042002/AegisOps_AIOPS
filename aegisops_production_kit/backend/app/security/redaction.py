"""Secret redaction for logs, SSE streams, console output, and context-graph writes.

Best-effort masking of common secret shapes. Defense-in-depth: secrets should never be
logged in the first place, but this guarantees streamed CLI/console output is masked.
"""

from __future__ import annotations

import re

_MASK = "••••REDACTED••••"

# Order matters; most specific first.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(-----BEGIN[^-]+PRIVATE KEY-----)(.*?)(-----END[^-]+PRIVATE KEY-----)", re.DOTALL),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._\-]+)"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)(\s*[=:]\s*)([^\s\"';,]+)"),
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


_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret|credential|private[_-]?key)")


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
