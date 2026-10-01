"""One reading of text, for everything that looks for an identifier in it.

A model writes "AB‑10010" with a non-breaking hyphen as readily as with an ASCII
one, and a customer types "ab-10003" as readily as "AB-10003". Every pattern
written for the store's own spelling was blind to both: the policy rules until
generation run 2 (AHC-0094), the watch until F-048, and the router and the
consent list until F-063. Each had grown its own fold, or none.

So the fold and the order-id shape live here, once, at the bottom layer where
every reader can reach them (AHC-0089: input shape is normalised by
deterministic code, not left to whoever reads next). What a customer is sent is
never rewritten by this module; it decides what an id *is*, not how a reply
looks.
"""

from __future__ import annotations

import re
import unicodedata

_FOLD = str.maketrans(
    {
        **dict.fromkeys("‐‑‒–—―−﹘﹣－", "-"),
        **dict.fromkeys("     ", " "),
        **dict.fromkeys("­​‌‍⁠﻿", None),
    }
)


def normalised(text: str) -> str:
    """Dashes to `-`, odd spaces to ` `, invisible characters gone, then NFKC."""
    return unicodedata.normalize("NFKC", text.translate(_FOLD))


ORDER_ID = re.compile(r"\b([A-Z]{1,3}-\d{3,8})\b")
"""The store's own spelling of an order id, and nothing else. Use it to ask
whether a string already *is* one; use `order_ids` to find them in text."""

_SPOKEN_ORDER_ID = re.compile(r"\b([A-Z]{1,3}|[a-z]{1,3})-(\d{3,8})\b")
"""An order id as people and models write it, after folding: upper or lower
case, never mixed — so "Rs-500" stays an amount and is not read as an order."""


def order_ids(text: str) -> set[str]:
    """Every order id in `text`, each in the store's spelling (`AB-10003`)."""
    return {
        f"{letters.upper()}-{digits}"
        for letters, digits in _SPOKEN_ORDER_ID.findall(normalised(text))
    }


__all__ = ["ORDER_ID", "normalised", "order_ids"]
