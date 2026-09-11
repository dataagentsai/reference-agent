"""The deterministic route's registry.

A router that names a handler the registry lacks would answer with a typed
failure on every such question — correct in shape, useless in fact. So the two
tables are checked against each other here, at build time, not discovered by a
customer.
"""

from __future__ import annotations

import pytest

from support_agent import router
from support_agent.contracts import Direct, Failed, Identity, Intent, RunId
from support_agent.entrypoint import direct


@pytest.mark.parametrize(
    ("intent", "name"), sorted(router.DIRECT_HANDLERS.items()), ids=lambda v: str(v)
)
def test_every_handler_the_router_names_is_registered(intent: Intent, name: str) -> None:
    assert name in direct.HANDLERS, f"the router routes {intent} to {name!r}, which is missing"


@pytest.mark.discharges("AHC-0017")
async def test_an_unregistered_handler_is_a_typed_failure_not_a_crash() -> None:
    decision = Direct(intent=Intent.ORDER_STATUS, handler="nonexistent", args={"order_id": "X"})
    result = await direct.answer(
        decision,
        Identity(customer_id="C-1"),
        RunId("r"),
        tools=None,
        handlers={},
    )
    assert isinstance(result, Failed)
    assert "nonexistent" in result.detail


# (order's status in the world, what a refund-status question must be told)
REFUND_STATES = [
    ("refunded", "A refund has been issued for order AB-10003, to the original payment method."),
    ("returned", "Your return for order AB-10003 has arrived, and the refund is being processed."),
    ("delivered", "There is no refund on order AB-10003."),
    ("shipped", "There is no refund on order AB-10003."),
]


@pytest.mark.discharges("P-REFUND-STATUS", "op:get_order")
@pytest.mark.parametrize(("status", "told"), REFUND_STATES, ids=[r[0] for r in REFUND_STATES])
async def test_a_refund_status_question_is_answered_about_the_refund(
    status: str, told: str
) -> None:
    """F-018: it used to be answered with the order's status."""
    from pathlib import Path

    from agenttwin import Live, load, project

    from support_agent import entrypoint as ep
    from support_agent import identity as ident
    from support_agent.contracts import Completed
    from support_agent.idempotency import InMemoryLedger
    from support_agent.llm import ScriptedClient
    from support_agent.state import InMemoryCheckpointStore
    from support_agent.tools import connect

    world = Live.start(load(Path(__file__).parent.parent / "worlds" / "clothing.yaml"))
    order = world.rows["order"]["AB-10003"]
    order["status"] = status
    if status not in ("delivered", "returned", "refunded"):
        order["days_since_delivery"] = 0  # a coherent order, not a fictional one (F-011)
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore())
        result, _ = await agent.handle(
            "what is happening with the refund for AB-10003",
            identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
        )
    assert isinstance(result, Completed) and result.reply == told
