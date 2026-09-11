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
