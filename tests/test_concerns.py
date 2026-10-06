"""A message's separate concerns, and what a person taking it is handed (T-093).

From a CCA-F case: three problems in one message, the first reply covered one,
and the human queue found no record of the others. Each clause is its own
concern, kept in the record, and listed in the handoff.
"""

from __future__ import annotations

import pytest

from support_agent.router import concerns
from support_agent.state.facts import Facts

THREE = (
    "The hinge on AB-10003 is cracked, I was charged twice for AB-10004, "
    "and is my laptop still under warranty?"
)

# [name, message, the concerns it raises]
SPLITS = [
    ("three problems, commas and an and", THREE,
     ("The hinge on AB-10003 is cracked", "I was charged twice for AB-10004",
      "is my laptop still under warranty?")),
    ("two sentences", "Cancel AB-10002. Where is my refund for AB-10003?",
     ("Cancel AB-10002", "Where is my refund for AB-10003?")),
    ("an and that joins two requests", "please cancel AB-10002 and where is my refund for AB-10003",
     ("please cancel AB-10002", "where is my refund for AB-10003")),
    ("an and inside one request stays whole", "return the shirt and trousers from AB-10003",
     ("return the shirt and trousers from AB-10003",)),
    ("small talk is not a concern", "Hi, thanks", ()),
    ("one plain question", "where is my order AB-10001", ("where is my order AB-10001",)),
]  # fmt: skip


@pytest.mark.discharges("AHC-0118", "P-CONCERNS")
@pytest.mark.parametrize(("name", "message", "expected"), SPLITS, ids=[s[0] for s in SPLITS])
def test_a_message_splits_into_its_concerns(
    name: str, message: str, expected: tuple[str, ...]
) -> None:
    assert concerns(message) == expected


@pytest.mark.discharges("AHC-0118", "AHC-0108", "AHC-0070")
def test_a_handoff_lists_every_concern_raised() -> None:
    handed = Facts().asking(THREE, concerns(THREE)).as_handoff()
    for concern in concerns(THREE):
        assert concern in handed
    assert "raised 3 things" in handed


@pytest.mark.discharges("AHC-0108")
def test_a_single_concern_is_not_listed_twice() -> None:
    handed = (
        Facts().asking("where is my order AB-10001", ("where is my order AB-10001",)).as_handoff()
    )
    assert "raised" not in handed
