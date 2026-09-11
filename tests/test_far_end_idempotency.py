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
from support_agent.tools import MCPTransport

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
CUSTOMER = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def key(iteration: int) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId("r-1"), step=1, iteration=iteration)


# (name, the keys the two calls carry, how many times the effect must land)
REPEATS = [
    ("the same key twice is one request", [key(0), key(0)], 1),
    ("a new key is a genuine second request", [key(0), key(1)], 2),
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
    if landed == 1:
        assert answers[0].structured == answers[1].structured, "a repeat gets the same answer"
