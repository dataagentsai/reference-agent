"""The separate concerns a message raises, and what the turn owes each (T-093).

AHC-0118, from a CCA-F case: customers open with several problems, the first
reply covered one, the others never came back, and the person who took the
conversation found no record of them. Each clause is a concern; a concern this
agent can act on, naming one order, is owed a call to one of its operations.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from support_agent.contracts import Agentic, Direct, Intent
from support_agent.contracts.reading import ORDER_ID, order_ids
from support_agent.router import route

_CLAUSE = re.compile(
    r"(?<=[.?!;])\s+|,\s*(?:and\s+)?|\s+and\s+(?=(?:i|i'm|i've|is|was|my|the|it|can|could"
    r"|please|where|what|when|how|why|also)\b)",
    re.I,
)


def concerns(text: str) -> tuple[str, ...]:
    """The separate things a message raises, one per clause (AHC-0118, T-093).

    A CCA-F case: customers open with several problems — a cracked hinge, a
    double charge, a warranty question — and the first reply covered one, and
    the person who later took the conversation found no record of the others.
    Split here so the record holds each, and a handoff carries each.

    A clause is a concern when it names an order or runs to three words:
    "Cancel AB-10002" is one, "thanks" and "that's all" are not.
    Splitting is by punctuation and a new clause after "and", which is crude and
    errs towards keeping a concern whole rather than cutting one in two.
    """
    parts = (part.strip(" ,.;") for part in _CLAUSE.split(text))
    return tuple(part for part in parts if ORDER_ID.search(part) or len(part.split()) >= 3)


VIA: dict[Intent, tuple[str, ...]] = {
    Intent.ORDER_STATUS: ("get_order", "list_orders"),
    Intent.REFUND_STATUS: ("get_order", "list_orders"),
    Intent.CANCEL_ORDER: ("cancel_order",),
    Intent.RETURN_REQUEST: ("open_return_request",),
    Intent.ADDRESS_CHANGE: ("change_address",),
    Intent.REFUND_REQUEST: ("request_refund",),
}
"""The AOAS intents' `via`: the operations that deal with each."""


class Owed(NamedTuple):
    """One concern the turn owes an outcome: its words, its order, what deals with it.
    A plain tuple, so the loop can read it without importing the router."""

    concern: str
    order: str
    tools: tuple[str, ...]


def owed(text: str) -> tuple[Owed, ...]:
    """The concerns of a several-concern message the loop must deal with (AHC-0118).

    Only where a clause names one order and carries one intent this agent can
    act on — what is owed is then a call to one of that intent's operations on
    that order, decided from the record and never from the reply's words. A
    single concern is the whole turn and needs no list.
    """
    clauses = concerns(text)
    if len(clauses) < 2:
        return ()
    out = []
    for clause in clauses:
        decision = route(clause)
        if isinstance(decision, Direct):
            intents: tuple[Intent, ...] = (decision.intent,)
        elif isinstance(decision, Agentic):
            intents = decision.candidate_intents
        else:
            intents = ()
        ids = order_ids(clause)
        if len(intents) == 1 and len(ids) == 1 and intents[0] in VIA:
            out.append(Owed(clause, next(iter(ids)), VIA[intents[0]]))
    return tuple(out)


__all__ = ["VIA", "Owed", "concerns", "owed"]
