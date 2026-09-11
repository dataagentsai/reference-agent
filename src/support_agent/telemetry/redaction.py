"""What never leaves the process: sensitive spans of text are replaced before export."""

from __future__ import annotations

import re

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{13,19}\b"), "[card]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b(?:\+91[- ]?)?[6-9]\d{9}\b"), "[phone]"),
    (re.compile(r"(?i)\b(sk|gsk|key)[-_][A-Za-z0-9]{8,}"), "[secret]"),
)


def redact(text: str, *, limit: int = 4000) -> str:
    """The single redaction point. Everything captured onto a span comes here.

    Deliberately crude: deterministic regexes, no model call, no network. A
    detector that can be wrong slowly is worse than one that is obviously
    approximate — this is a floor, not a compliance control.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return text[:limit] + f"…[truncated {len(text) - limit} chars]"
    return text
