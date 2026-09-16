"""What never leaves the process: sensitive spans of text are replaced before export.

**AAC-0095**, and AOAS `personal_data_in_conversation` — card numbers typed
into the chat are
personal data that no field declares, so nothing else in the system knows to
protect them. Declared fields carry `pii: true` and are handled where they are
declared; this is the other half, and it runs at the one boundary where text
leaves for a backend somebody else operates.
"""

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
