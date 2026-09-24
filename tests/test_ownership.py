"""F-016 — a customer may see and act on only the orders they placed.

Two customers, which is the whole point: the defect survived because every
test, scenario and golden case had one. The rule is AOAS `P-OWNERSHIP`, enforced
where the tool executes — the order system reads the caller's session from the
call and answers a stranger exactly as it answers a request for an order that
does not exist, so a guess learns nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.contracts import Failed, IdempotencyKey, Identity, RunId
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
OWNER = "C-1042"
STRANGER = "C-9999"


def caller(customer_id: str) -> Identity:
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


# (operation, order it acts on, arguments) — every order operation a customer holds
OPERATIONS = [
    ("get_order", "AB-10003", {"id": "AB-10003"}),
    ("cancel_order", "AB-10002", {"id": "AB-10002"}),
    ("open_return_request", "AB-10003", {"id": "AB-10003"}),
    ("change_address", "AB-10002", {"id": "AB-10002", "address": "12 New Road, Pune"}),
]
CASES = [
    (f"{who} {op}", op, order, args, customer_id, customer_id == OWNER)
    for op, order, args in OPERATIONS
    for who, customer_id in (("owner", OWNER), ("stranger", STRANGER))
]


@pytest.mark.discharges(
    "P-OWNERSHIP",
    "AHC-0040",
    "AAC-0057",
    "ext:order_system",
    "op:get_order",
    "op:cancel_order",
    "op:open_return_request",
    "op:change_address",
)
@pytest.mark.parametrize(
    ("name", "operation", "order", "arguments", "customer_id", "owns"),
    CASES,
    ids=[c[0] for c in CASES],
)
async def test_only_the_owner_reaches_the_order(
    name: str, operation: str, order: str, arguments: dict, customer_id: str, owns: bool
) -> None:
    world = Live.start(load(WORLD))
    before = world.snapshot()
    key = IdempotencyKey(run_id=RunId("r"), step=1, iteration=0)
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        result = await tools.call(operation, arguments, caller(customer_id), key)
        # What the owner is told about an order that does not exist at all.
        absent = await tools.call(operation, {**arguments, "id": "AB-00000"}, caller(OWNER), key)

    if owns:
        assert not result.is_error, result.text
        return
    assert result.is_error, "a stranger was served"
    told = (result.text, result.error_channel, result.structured)
    assert told == (absent.text, absent.error_channel, absent.structured), (
        "not yours must read exactly as not there — anything else confirms the guess"
    )
    assert world.snapshot() == before and world.effects == [], "the world did not move"


@pytest.mark.discharges("P-OWNERSHIP", "R-OTHER-CUSTOMER", "op:get_order")
async def test_a_stranger_asking_after_an_order_learns_nothing() -> None:
    """The deterministic route answers without the model — and so, until the
    order system checked, it answered anyone's question about anyone's order."""
    world = Live.start(load(WORLD))
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore())
        result, _ = await agent.handle("where is my order AB-10003", identity=caller(STRANGER))

    assert isinstance(result, Failed), result
    assert "delivered" not in result.customer_message
