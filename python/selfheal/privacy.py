"""Best-effort redaction. Arbitrary personal data needs an application policy."""
from __future__ import annotations

import os
import re

_SENSITIVE = re.compile(r"key|token|secret|password|credential", re.I)
_ASSIGNMENT = re.compile(
    r'''(?i)([\w-]*(?:api[_-]?key|token|secret|password|credential)[\w-]*["']?\s*[:=]\s*)(["'])([^\r\n]*?)\2'''
)
_BARE = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*)(?![\"'])([^\s,;]+)"
)
_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,})\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    values = set(secrets)
    values.update(v for k, v in os.environ.items() if _SENSITIVE.search(k) and len(v) >= 8)
    for value in sorted(values, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    text = _KEY.sub("[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _ASSIGNMENT.sub(lambda m: m[1] + m[2] + "[REDACTED]" + m[2], text)
    return _BARE.sub(lambda m: m[1] + "[REDACTED]", text)
