"""The deterministic route's registry.

A router that names a handler the registry lacks would answer with a typed
failure on every such question — correct in shape, useless in fact. So the two
tables are checked against each other here, at build time, not discovered by a
customer.
"""

from __future__ import annotations

import pytest

from support_agent import router
from support_agent.contracts import (
    Completed,
    Direct,
    Failed,
    Identity,
    Intent,
    RunId,
    SideEffectClass,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from support_agent.entrypoint import direct


@pytest.mark.parametrize(
    ("intent", "name"), sorted(router.DIRECT_HANDLERS.items()), ids=lambda v: str(v)
)
@pytest.mark.discharges("P-DIRECT")
def test_every_handler_the_router_names_is_registered(intent: Intent, name: str) -> None:
    assert name in direct.HANDLERS, f"the router routes {intent} to {name!r}, which is missing"


# (what the customer said, whether the deterministic path may take it)
# F-037: only the "one intent" half of `P-DIRECT` was checked. The turn naming
# three orders matched one intent, carried an order id, and was answered about
# the first of them with nothing said about the other two.
SUBJECTS = [
    ("one order", "where is my order AB-10003", True),
    ("the same order twice", "where is AB-10003, I mean AB-10003", True),
    ("two orders", "where is my order AB-10003 and what about AB-10004", False),
    ("three orders", "where are AB-10003, AB-10004 and AB-10005", False),
    ("no order at all", "where is my order", False),
]


@pytest.mark.discharges("P-DIRECT")
@pytest.mark.parametrize(("name", "said", "deterministic"), SUBJECTS, ids=[c[0] for c in SUBJECTS])
def test_the_deterministic_path_needs_one_intent_and_one_order(
    name: str, said: str, deterministic: bool
) -> None:
    """A path that has to *choose* which order was meant is not deterministic —
    and it does not choose out loud, it picks the first one. The repeated id is
    the case worth keeping: the same order named twice is still one order, so the
    rule counts subjects and not matches."""
    decision = router.route(said)
    assert isinstance(decision, Direct) is deterministic, decision


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
    (
        "returned",
        "Your return for order AB-10003 has arrived, and a refund is owed on it. "
        "Ask me to refund it and I will request it now.",
    ),
    ("delivered", "There is no refund on order AB-10003."),
    ("shipped", "There is no refund on order AB-10003."),
]


@pytest.mark.discharges("P-REFUND-STATUS", "P-REFUND-OWED", "op:get_order")
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
    from support_agent.llm import ScriptedClient
    from support_agent.requests import InMemoryRequests
    from support_agent.state import InMemoryCheckpointStore
    from support_agent.tools import connect

    world = Live.start(load(Path(__file__).parent.parent / "worlds" / "clothing.yaml"))
    order = world.rows["order"]["AB-10003"]
    order["status"] = status
    if status not in ("delivered", "returned", "refunded"):
        order["days_since_delivery"] = 0  # a coherent order, not a fictional one (F-011)
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore())
        result, _ = await agent.handle(
            "what is happening with the refund for AB-10003",
            identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
        )
    assert isinstance(result, Completed) and result.reply == told
    # Asking about a refund is not asking for one, even where one is owed.
    assert world.count("issue_refund") == 0


class _AnswersNotFound:
    """An order system that answers as the real store does for an order that is
    missing or is not this customer's: `found: false`, a successful call."""

    async def list_tools(self, identity: Identity) -> ToolRegistry:
        return ToolRegistry(
            tools=(
                ToolSpec(
                    name=direct.LOOKUP_TOOL,
                    description="Look up an order's current status.",
                    input_schema={
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                    },
                    output_schema={"type": "object"},
                    side_effect=SideEffectClass.READ,
                ),
            )
        )

    async def call(
        self, name: str, arguments: dict[str, object], identity: Identity, idempotency_key: object
    ) -> ToolResult:
        return ToolResult(name=name, structured={"found": False, "id": arguments["id"]})


# (handler, what the customer asked) — F-062: both answered about an order that
# the order system said is not there, "currently unknown" and "no refund".
NOT_FOUND = [
    ("order_status", Intent.ORDER_STATUS),
    ("refund_status", Intent.REFUND_STATUS),
]


@pytest.mark.discharges("P-DIRECT", "AHC-0086")
@pytest.mark.parametrize(("handler", "intent"), NOT_FOUND, ids=[c[0] for c in NOT_FOUND])
async def test_an_order_the_system_did_not_find_is_not_described(
    handler: str, intent: Intent
) -> None:
    """`found: false` is an answer about absence, not an order with no status.
    The reply says the order was not found on this account, and says nothing
    the order system did not return."""
    decision = Direct(intent=intent, handler=handler, args={"order_id": "AB-10003"})
    result = await direct.answer(
        decision, Identity(customer_id="C-7001"), RunId("r"), tools=_AnswersNotFound()
    )
    assert isinstance(result, Completed), result
    assert result.reply == "I could not find order AB-10003 on your account.", result.reply


# (how the customer wrote the order id) — F-063: only the store's own spelling
# was recognised, so each of these skipped the direct route and paid for a model
# call, and the consent list granted nothing on them.
SPELLINGS = [
    ("the store's spelling", "where is my order AB-10003"),
    ("lower case", "where is my order ab-10003"),
    ("non-breaking hyphen, as the agent writes it (F-048)", "where is my order AB‑10003"),
    ("en dash", "where is my order AB–10003"),
    ("full-width", "where is my order ＡＢ－１０００３"),
    ("a soft hyphen hidden inside", "where is my order AB-­10003"),
]


@pytest.mark.discharges("P-DIRECT", "AHC-0089")
@pytest.mark.parametrize(("name", "said"), SPELLINGS, ids=[c[0] for c in SPELLINGS])
def test_an_order_id_is_recognised_however_it_is_written(name: str, said: str) -> None:
    decision = router.route(said)
    assert isinstance(decision, Direct), decision
    assert decision.args == {"order_id": "AB-10003"}


# (what was written, whether it is an order id) — the fold must not invent ids.
NOT_ORDERS = [
    ("an amount in mixed case", "I paid Rs-500 for it"),
    ("a too-short number", "where is AB-12"),
]


@pytest.mark.discharges("AHC-0089")
@pytest.mark.parametrize(("name", "said"), NOT_ORDERS, ids=[c[0] for c in NOT_ORDERS])
def test_the_fold_reads_no_order_id_where_there_is_none(name: str, said: str) -> None:
    from support_agent.contracts.reading import order_ids

    assert order_ids(said) == set()
