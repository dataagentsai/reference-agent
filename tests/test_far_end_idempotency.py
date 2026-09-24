"""F-017 — the idempotency key reaches the system the effect lands on.

The case the harness's ledger cannot cover: the effect landed, the reply was
lost, so the ledger never recorded it and the retry goes through. Only the far
end can recognise it. These call the transport directly — below the ledger —
which is exactly the position a lost reply leaves the retry in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project
from mcp.client import Client

from support_agent import identity as ident
from support_agent.contracts import IdempotencyKey, Identity, RunId
from support_agent.requests import InMemoryRequests
from support_agent.tools import MCPTransport, connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
CUSTOMER = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
RETURNABLE = "AB-10003"


def key(iteration: int) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId("r-1"), step=1, iteration=iteration)


# (name, the keys the two calls carry, how many times the effect must land)
REPEATS = [
    ("the same key twice is one request", [key(0), key(0)], 1),
    # A new key is judged again rather than replayed, and the order refuses it:
    # `return_open` is state the order system holds, so `identity: order_id` is
    # answered by the record. Until T-050 the effect was prose, this row said 2,
    # and a live model that retried a lost reply as a new call opened two returns.
    ("a new key is judged again, and the order refuses a second return", [key(0), key(1)], 1),
]


@pytest.mark.discharges("ext:order_system", "op:open_return_request", "AHC-0074", "AAC-0047")
@pytest.mark.parametrize(("name", "keys", "landed"), REPEATS, ids=[r[0] for r in REPEATS])
async def test_the_far_end_recognises_a_retried_write(
    name: str, keys: list[IdempotencyKey], landed: int
) -> None:
    world = Live.start(load(WORLD))
    async with Client(project(world)) as client:
        transport = MCPTransport(client)
        answers = [
            await transport.invoke(
                "open_return_request", {"id": "AB-10003"}, caller=CUSTOMER, idempotency_key=k
            )
            for k in keys
        ]

    assert world.count("open_return_request") == landed
    if keys[0] == keys[1]:
        assert answers[0].structured == answers[1].structured, "a repeat gets the same answer"
    else:
        assert answers[1].structured["allowed"] is False, (
            "a second request is refused, not replayed"
        )


@pytest.mark.discharges("op:issue_refund", "P-REFUND", "AHC-0074", "AAC-0047")
async def test_a_second_refund_under_a_fresh_key_is_refused_by_the_order_system() -> None:
    """The key and the domain answer different questions, and both are answered.

    A harness key asks *is this the same call*: a fresh key is honestly a second
    call, and the ledger lets it through — which is right, and is what
    `test_a_later_iteration_is_a_genuine_second_execution` pins. The AOAS's
    `identity: order_id` asks *is this the same request*, and the system that
    owns the record answers it: `status != refunded` refuses the second refund
    however new the key is. A harness holding only the key would pay twice for
    two genuine attempts.
    """
    live = Live.start(load(WORLD))
    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE})
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        first = await tools.call("issue_refund", {"id": RETURNABLE}, who, key(0))
        second = await tools.call("issue_refund", {"id": RETURNABLE}, who, key(1))

    assert first.structured["allowed"] is True, first.structured
    assert second.structured["allowed"] is False, "a fresh key bought a second refund"
    assert live.count("issue_refund") == 1
