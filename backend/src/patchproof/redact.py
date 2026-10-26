"""Secret redaction for logs, error messages, and anything echoed back to the client."""

from __future__ import annotations

import os
import re

_PATTERNS = [
    re.compile(r"bc_(?:live|test)_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[\"']?[^\s\"']{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
]


def redact(text: str | None, extra_secrets: tuple[str, ...] = ()) -> str:
    if not text:
        return text or ""
    secrets = [s for s in extra_secrets if s and len(s) >= 6]
    env_key = os.environ.get("BHARATCODE_API_KEY")
    if env_key and len(env_key) >= 6:
        secrets.append(env_key)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text
