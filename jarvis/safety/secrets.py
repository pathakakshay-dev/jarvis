"""High-confidence credential shapes. This is not a complete secret detector."""

from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bsecret\s*=\s*\S{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def looks_like_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATTERNS)


def redact_known_secrets(text: str) -> str:
    redacted = text
    for pattern in _PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted
