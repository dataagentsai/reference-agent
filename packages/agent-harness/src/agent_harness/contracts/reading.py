"""One reading of text, for everything that looks for an identifier in it.

A model writes an identifier with a non-breaking hyphen as readily as with an
ASCII one, and a customer types it in lower case as readily as upper. Every
pattern written for the canonical spelling was blind to both, until each reader
grew its own fold, or none. So the fold lives here, once, at the bottom layer
where every reader can reach it (AHC-0089: input shape is normalised by
deterministic code, not left to whoever reads next).

**What an identifier looks like, and which states a sentence can assert, are an
agent's.** A shop's order ids and an insurer's claim numbers are different
shapes, and "has shipped" is not a claim an insurer's reply makes. The harness
modules that need them — the superseded-state rule and the watch's outcomes —
read them through `vocabulary()`, which an agent fills once at import with
`use_vocabulary`. With none registered, nothing is an identifier and no
sentence asserts a state, so those checks find nothing rather than guessing.

What a customer is sent is never rewritten by this module; it decides what an
id *is*, not how a reply looks.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

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


class ClaimedStates(Protocol):
    def __call__(self, sentence: str, *, present_only: bool = False) -> set[str]: ...


def _none_found(text: str) -> set[str]:
    return set()


def _no_states(sentence: str, *, present_only: bool = False) -> set[str]:
    return set()


@dataclass(frozen=True)
class Vocabulary:
    """What an agent's identifiers look like, and the states its replies assert."""

    identifier: re.Pattern[str]
    """An identifier in its canonical spelling, and nothing else — to ask whether
    a string already *is* one, or to find the canonical ones in a sentence."""
    identifiers: Callable[[str], set[str]]
    """Every identifier in a text as people and models write it, canonicalised."""
    claimed_states: ClaimedStates
    """The states a sentence asserts of something; `present_only` drops the past."""


NOTHING = Vocabulary(
    identifier=re.compile(r"(?!)"), identifiers=_none_found, claimed_states=_no_states
)
"""The vocabulary before an agent registers one: no identifiers, no states."""

_registered: list[Vocabulary] = [NOTHING]


def use_vocabulary(vocabulary: Vocabulary) -> None:
    """Register the agent's vocabulary. The last registration wins."""
    _registered[:] = [vocabulary]


def vocabulary() -> Vocabulary:
    """The registered vocabulary, read at call time."""
    return _registered[0]


__all__ = [
    "NOTHING",
    "ClaimedStates",
    "Vocabulary",
    "normalised",
    "use_vocabulary",
    "vocabulary",
]
