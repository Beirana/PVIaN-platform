from __future__ import annotations

import re
from collections.abc import Iterable

AUTHORIZATION_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+")
KEY_VALUE_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*)[^\s,;]+"
)


def redact_text(
    value: object,
    *,
    secrets: Iterable[str] = (),
    max_length: int = 500,
) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", text)
    text = KEY_VALUE_PATTERN.sub(r"\1[REDACTED]", text)
    return text if len(text) <= max_length else text[:max_length] + "…"
