"""What never leaves the process: sensitive spans of text are replaced before export.

**AAC-0095**, and AOAS `personal_data_in_conversation` — card numbers typed
into the chat are
personal data that no field declares, so nothing else in the system knows to
protect them. Declared fields carry `pii: true` and are handled where they are
declared; this is the other half, and it runs at the one boundary where text
leaves for a backend somebody else operates.

**India's two identity numbers** (claims-fnol-azure A12). An Aadhaar number is
12 digits, first digit 2–9, written whole or 4-4-4, and its last digit is a
Verhoeff check digit; only a number whose check digit holds is masked, so a
random 12-digit figure (an account, a reference) is not called an Aadhaar
number. About one in ten random 12-digit numbers still passes the check: this is
a floor, not a classifier. A PAN is five letters, four digits and a letter; its
fourth letter says who holds it (P a person, C a company, …), and only those
ten letters are taken, in either case.

**An agent adds its own** with `register`: a bank account or a driving licence
is personal data for one agent and a product code for another (FINDINGS F-10 in
claims-fnol-azure, which rebound a private tuple to get there). Registered
patterns run after the library's, in the order given; registering one twice
adds it once.
"""

from __future__ import annotations

import re
from collections.abc import Callable

Redaction = tuple[re.Pattern[str], str]
"""A pattern, and what each match is replaced with."""

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    """Whether `digits` ends in a correct Verhoeff check digit (UIDAI's scheme)."""
    check = 0
    for i, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(digit)]]
    return check == 0


AADHAAR = re.compile(r"(?<![\d-])(?<!\d )[2-9]\d{3}([ -]?)\d{4}\1\d{4}(?![\d-])(?!\1\d)")
"""12 digits not starting 0 or 1, whole or in fours (one separator throughout)."""
PAN = re.compile(r"(?i)\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")
"""Five letters, the fourth the holder's kind; four digits; a letter."""


def aadhaar_numbers(text: str) -> list[str]:
    """The Aadhaar-shaped numbers in `text` whose check digit holds."""
    return [m.group(0) for m in AADHAAR.finditer(text) if verhoeff_valid(_digits(m.group(0)))]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _aadhaar(match: re.Match[str]) -> str:
    return "[aadhaar]" if verhoeff_valid(_digits(match.group(0))) else match.group(0)


_LIBRARY: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (AADHAAR, _aadhaar),
    (PAN, "[pan]"),
    (re.compile(r"\b\d{13,19}\b"), "[card]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b(?:\+91[- ]?)?[6-9]\d{9}\b"), "[phone]"),
    (re.compile(r"(?i)\b(sk|gsk|key)[-_][A-Za-z0-9]{8,}"), "[secret]"),
    # A signed token (JWT) and whatever follows "Bearer": a far end's error can
    # quote the credential it refused (F-074).
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*"), "[token]"),
    (re.compile(r"(?i)\bbearer\s+(?!\[token\])\S+"), "Bearer [token]"),
)
_registered: list[Redaction] = []


def register(*redactions: Redaction) -> None:
    """Add an agent's own patterns, after the library's. Idempotent per pattern."""
    for redaction in redactions:
        if redaction not in _registered:
            _registered.append(redaction)


def registered() -> tuple[Redaction, ...]:
    """The agent's patterns, in the order they run."""
    return tuple(_registered)


def redact(text: str, *, limit: int = 4000) -> str:
    """The single redaction point. Everything captured onto a span comes here.

    Deliberately crude: deterministic regexes, no model call, no network. A
    detector that can be wrong slowly is worse than one that is obviously
    approximate — this is a floor, not a compliance control.
    """
    for pattern, replacement in (*_LIBRARY, *_registered):
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return text[:limit] + f"…[truncated {len(text) - limit} chars]"
    return text


__all__ = [
    "AADHAAR",
    "PAN",
    "Redaction",
    "aadhaar_numbers",
    "redact",
    "register",
    "registered",
    "verhoeff_valid",
]
