"""P-OPEN (T-001): what a customer is shown on opening a conversation.

Each row sets up one thing about the world or the agent's queues and states what
the opening must and must not show. Every row runs the real agent over the
projected shop with a model that raises if it is called, and checks no model
span was emitted either: opening costs a read and a string.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.binding import SCOPES
from support_agent.contracts import Approval, Escalation, Identity
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def customer(customer_id: str) -> Identity:
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


def someone_elses_order(live: Live) -> None:
    """A second customer with an order, so scoping is tested by what is left out."""
    live.rows["order"]["AB-90001"] = {
        **live.rows["order"]["AB-10001"],
        "id": "AB-90001",
        "customer_id": "C-9999",
    }


def pending_refund(customer_id: str) -> Approval:
    return Approval(
        id=f"apr_{customer_id}",
        action="issue_refund",
        args={"order_id": "AB-10003", "amount": "4999"},
        reason="over the threshold",
        customer_id=customer_id,
        idempotency_key="run_x:1:1",
        created_at=0,
        expires_at=10**12,
    )


def queued_case(customer_id: str) -> Escalation:
    return Escalation(
        id=f"E-{customer_id}",
        conversation_id="cnv_x",
        run_id="run_x",
        customer_id=customer_id,
        rule_id="asked-for-human",
        rules_version="v1",
        reason="asked for a person",
        created_at=0,
        expires_at=10**12,
    )


async def open_as(who: str, *, setup=(), approvals=(), escalations=(), down: bool = False) -> str:
    live = Live.start(load(WORLD))
    for step in setup:
        step(live)
    approval_store, escalation_store = ap.InMemoryApprovalStore(), esc.InMemoryEscalationStore()
    for a in approvals:
        await approval_store.put(a)
    for e in escalations:
        await escalation_store.put(e)

    def refuse(name, fn):
        if not down:
            return fn

        async def unavailable(*args, **kwargs):
            raise RuntimeError("the order system is not answering")

        unavailable.__signature__ = fn.__signature__
        unavailable.__annotations__ = fn.__annotations__
        return unavailable

    server = project(live, scopes=SCOPES, wrap=refuse)
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approval_store,
            escalations=escalation_store,
        )
        return await agent.opening(customer(who))


# (why, who opens, how it is set up, shown, not shown)
ROWS = [
    (
        "a customer sees their own orders and statuses",
        "C-1042",
        {},
        ["AB-10003: delivered", "AB-10002: pending", "Which one can I help with"],
        [],
    ),
    (
        "and not another customer's",
        "C-1042",
        {"setup": [someone_elses_order]},
        ["AB-10001"],
        ["AB-90001"],
    ),
    (
        "the other customer sees only theirs",
        "C-9999",
        {"setup": [someone_elses_order]},
        ["AB-90001"],
        ["AB-10003", "AB-10001:"],
    ),
    ("a customer with no orders is told so", "C-9999", {}, ["no orders with us yet"], ["AB-"]),
    (
        "a refund waiting on a colleague is shown",
        "C-1042",
        {"approvals": [pending_refund("C-1042")]},
        ["Already in hand", "issue refund for AB-10003, waiting for a colleague"],
        [],
    ),
    (
        "a case with the desk is shown",
        "C-1042",
        {"escalations": [queued_case("C-1042")]},
        ["E-C-1042, with a colleague"],
        [],
    ),
    (
        "another customer's work in flight is not",
        "C-1042",
        {"approvals": [pending_refund("C-9999")], "escalations": [queued_case("C-9999")]},
        [],
        ["Already in hand", "E-C-9999", "apr_C-9999"],
    ),
    (
        "an order system that will not answer is said plainly",
        "C-1042",
        {"down": True},
        ["cannot see your orders right now"],
        ["AB-10003"],
    ),
]


@pytest.mark.parametrize(("why", "who", "setup", "shown", "hidden"), ROWS, ids=[r[0] for r in ROWS])
@pytest.mark.discharges("P-OPEN", "P-OWNERSHIP", "op:list_orders")
async def test_opening_shows_the_customers_own_orders_and_work(
    exporter, why: str, who: str, setup: dict, shown: list[str], hidden: list[str]
) -> None:
    text = await open_as(who, **setup)

    for expected in shown:
        assert expected in text, text
    for absent in hidden:
        assert absent not in text, text
    spans = [s.name for s in exporter.get_finished_spans()]
    assert "agent.opening" in spans
    assert not any(name.startswith("gen_ai") for name in spans), "opening asked a model"


@pytest.mark.discharges("P-OPEN")
async def test_a_long_history_is_summarised_not_dumped() -> None:
    def many(live: Live) -> None:
        for n in range(8):
            live.rows["order"][f"AB-2000{n}"] = {
                **live.rows["order"]["AB-10001"],
                "id": f"AB-2000{n}",
            }

    text = await open_as("C-1042", setup=[many])
    assert text.count("\n- AB-") == 5
    assert "and 9 more" in text
