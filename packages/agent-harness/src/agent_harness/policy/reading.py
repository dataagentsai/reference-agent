"""What a rule reads: the model's text with look-alike characters folded.

A model writes "AB‑10010" with a non-breaking hyphen as readily as with an ASCII
one, and every rule matching an identifier was blind to it: a promised refund on
that order passed the checks because the order it named did not look like one
(generation run 2, AHC-0094). So every rule reads one spelling. Only the rules'
copy is folded; what the customer is sent is never rewritten.

The fold itself lives in `contracts.reading`, shared with the router, the
consent list and the watch, so there is one definition of a look-alike (F-063).
"""

from __future__ import annotations

from agent_harness.contracts.reading import normalised

__all__ = ["normalised"]
