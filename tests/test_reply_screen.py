"""F-020 — every route's reply passes the reply guardrails, not only the model's.

Each row plants a bad reply on one route that never reaches the model — the kind
of template edit nobody screens because "this path is safe" — and asserts the
customer never reads it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agenttwin import Live, load, project
from evals import durable

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import policy as pol
from support_agent import router
from support_agent.contracts import Escalated, Identity, Refused
from support_agent.entrypoint import direct
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
CUSTOMER = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
DISCOUNTING = router.Rules(
    refuse=(("R-DISCOUNT", "take 20% off instead", re.compile(r"\bdiscount\b", re.I)),),
)


def plant_refusal(monkeypatch: pytest.MonkeyPatch) -> dict:
    return {"rules": DISCOUNTING}


def plant_status(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(direct, "STATUS_REPLY", "Order {{ order_id }}: use coupon code SAVE20.")
    return {}


def plant_handoff(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(esc, "RAISED_REPLY", "Card 4111111111111111 is on file — ref {ticket}.")
    return {"escalations": durable.RememberedEscalations()}


# (route, what the customer says, how the bad reply is planted, the rule that must catch it)
ROUTES = [
    ("refusal", "can I have a discount", plant_refusal, "no_discount_offer"),
    ("deterministic status", "where is my order AB-10003", plant_status, "no_discount_offer"),
    ("escalation desk", "I want to speak to a person", plant_handoff, "no_pii_echo"),
]


@pytest.mark.discharges("AHC-0094", "AAC-0099", "R-DISCOUNT")
@pytest.mark.parametrize(("route", "text", "plant", "rule"), ROUTES, ids=[r[0] for r in ROUTES])
async def test_no_route_reaches_the_customer_unscreened(
    monkeypatch: pytest.MonkeyPatch, route: str, text: str, plant, rule: str
) -> None:
    wiring = plant(monkeypatch)
    async with connect(project(Live.start(load(WORLD))), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore(), **wiring
        )
        result, conversation = await agent.handle(text, identity=CUSTOMER)

    told = getattr(result, "reply", None) or getattr(result, "customer_message", "")
    assert told == pol.SAFE_REPLY, f"{route} reached the customer unscreened: {told!r}"
    if isinstance(result, Escalated):
        assert conversation.pending_escalation_id == result.ticket_id, "the handoff survived"


# (name, the route's result kind before screening, the type the customer's caller sees)
BLOCKED = [
    ("a completion is a refusal — it carried nothing but the words", "completed", "Refused"),
    ("an escalation stays an escalation — the handoff happened", "escalated", "Escalated"),
]


@pytest.mark.discharges("AHC-0017", "AHC-0094", "AAC-0099")
@pytest.mark.parametrize(("name", "kind", "expected"), BLOCKED, ids=[b[0] for b in BLOCKED])
async def test_a_blocked_reply_is_typed_as_what_it_now_is(
    monkeypatch: pytest.MonkeyPatch, name: str, kind: str, expected: str
) -> None:
    """F-026: a blocked completion came back as `Completed` with a `REFUSED`
    termination, so a caller branching on the type read a refusal as a success.
    The types that record something the turn *did* keep theirs: replacing an
    `Escalated` would drop the handoff the blocked reply was about."""
    plant = plant_status if kind == "completed" else plant_handoff
    text = "where is my order AB-10003" if kind == "completed" else "I want to speak to a person"
    wiring = plant(monkeypatch)

    async with connect(project(Live.start(load(WORLD))), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore(), **wiring
        )
        result, _ = await agent.handle(text, identity=CUSTOMER)

    assert type(result).__name__ == expected, result
    assert result.reply == pol.SAFE_REPLY
    if isinstance(result, Refused):
        assert result.rule_id == "no_discount_offer", "which rule refused it, not just that one did"
