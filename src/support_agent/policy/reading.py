"""What a rule reads: the model's text with look-alike characters folded.

A model writes "AB‑10010" with a non-breaking hyphen as readily as with an ASCII
one, and every rule matching an identifier was blind to it: a promised refund on
that order passed the checks because the order it named did not look like one
(generation run 2, AHC-0094). So every rule reads one spelling. Only the rules'
copy is folded; what the customer is sent is never rewritten.
"""

from __future__ import annotations

import unicodedata

_FOLD = str.maketrans(
    {
        **dict.fromkeys("‐‑‒–—―−﹘﹣－", "-"),
        **dict.fromkeys("     ", " "),
        **dict.fromkeys("­​‌‍⁠﻿", None),
    }
)


def normalised(text: str) -> str:
    """Dashes to `-`, odd spaces to ` `, invisible characters gone, then NFKC."""
    return unicodedata.normalize("NFKC", text.translate(_FOLD))


__all__ = ["normalised"]
