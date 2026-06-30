"""Confidentiality classifier — Low/Medium/High + score for every agent message.

A real pattern + heuristic classifier over the response/content (no raw secrets are stored).
Drives the badge the UI shows on each agent message. Deterministic and offline; an optional
LLM check can be layered in later without changing the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (compiled pattern, weight, label)
_SIGNALS: list[tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"-----BEGIN[^-]+PRIVATE KEY-----"), 1.0, "private_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), 0.9, "aws_key"),
    (re.compile(r"(?i)\b(password|secret|token|api[_-]?key|client[_-]?secret|credential)\b"), 0.5, "secret_term"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."), 0.9, "jwt"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0.8, "ssn"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), 0.6, "card_number"),
    (re.compile(r"(?i)\b(production|prod)\b"), 0.25, "production"),
    (re.compile(r"(?i)\b(iam|rbac|policy|kms|encryption)\b"), 0.2, "security_config"),
    (re.compile(r"(?i)\b(invoice|revenue|salary|financial|spend|cost)\b"), 0.2, "financial"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), 0.15, "email"),
]


@dataclass
class Confidentiality:
    level: str
    score: float
    signals: list[str]


def classify(text: str) -> Confidentiality:
    if not text:
        return Confidentiality(level="Low", score=0.0, signals=[])
    score = 0.0
    hits: list[str] = []
    for pat, weight, label in _SIGNALS:
        if pat.search(text):
            score += weight
            hits.append(label)
    score = min(1.0, round(score, 3))
    level = "High" if score >= 0.7 else "Medium" if score >= 0.3 else "Low"
    return Confidentiality(level=level, score=score, signals=hits)
