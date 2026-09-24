"""The agent's tools, against the real store (T-017).

    docker compose --profile store up -d saleor
    uv run python deploy/saleor/seed.py

The agent's own `connect()` and `ToolClient`, the far end's own checks, and
Saleor behind them. What is being tested is the *swap*: the tool surface a
scenario drives is the same surface, and every answer means the same thing.

Skipped when no Saleor answers. Each test leaves the store as it found it, or
names what it changed — these run against a store that other tests read.
"""

from __future__ import annotations

import os
import time
import urllib.error
import uuid
from pathlib import Path

import pytest
import yaml
from deploy.saleor import seed as seeding
from evals import durable
from evals import issuer as issuing

from order_system import server as store_server
from order_system.store import Saleor, Store
from support_agent import identity as ident
from support_agent.contracts import IdempotencyKey, Identity, RunId
from support_agent.requests import InMemoryRequests
from support_agent.tools import connect

URL = os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")
CUSTOMER = "C-1042"
STRANGER = "C-9999"


def saleor() -> Saleor:
    return Saleor(
        URL,
        os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com"),
        os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only"),
    )


@pytest.fixture(scope="module")
def store() -> Store:
    """A private copy of the world: the shared one is a person's, and they
    change it (AB-10002 was cancelled in the first session)."""
    try:
        api = seeding.login(URL, saleor().email, saleor().password)
    except (urllib.error.URLError, seeding.SaleorError, OSError) as exc:
        pytest.skip(f"no Saleor at {URL}: {exc}")
    namespace = f"t{uuid.uuid4().hex[:6]}"
    world = yaml.safe_load((Path(__file__).parent.parent / "worlds" / "clothing.yaml").read_text())
    seeding.seed(api, world, now=int(time.time()), namespace=namespace)
    return Store(saleor(), namespace=namespace)


def session(customer: str) -> Identity:
    """A customer's session, verified, as the chat edge hands it on."""
    return ident.verify(issuing.mint(customer), issuer=issuing.issuer()).as_customer()


def key(step: int) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId(f"run_store_{int(time.time())}"), step=step, iteration=0)


def tools_for(store: Store, approvals=None):
    server = store_server.build(
        store,
        issuer=issuing.issuer(audience="order-system"),
        clock=lambda: int(time.time()),
        approvals=approvals or durable.Remembered(),
    )
    return connect(server, requests=InMemoryRequests(), exchange=issuing.LocalExchange())


# (why, the order, the tool, what the agent's surface must conclude)
READS = [
    ("an order of theirs is found", "AB-10003", CUSTOMER, True),
    ("an order that is not theirs is not found", "AB-10003", STRANGER, False),
    ("an order nobody has is not found", "AB-99999", CUSTOMER, False),
]


@pytest.mark.parametrize(("why", "order", "who", "found"), READS, ids=[r[0] for r in READS])
@pytest.mark.discharges("op:get_order", "P-OWNERSHIP", "AAC-0057")
async def test_reading_an_order_from_the_real_store(
    store: Store, why: str, order: str, who: str, found: bool
) -> None:
    async with tools_for(store) as tools:
        result = await tools.call("get_order", {"id": order}, session(who), key(1))

    assert result.structured["found"] is found, result.text
    if found:
        assert result.structured["status"] == "delivered"
        assert result.structured["total"] == 4999
        assert result.structured["days_since_delivery"] >= 0


@pytest.mark.discharges("op:list_orders", "P-OWNERSHIP", "P-OPEN")
async def test_a_customer_lists_only_their_own_orders(store: Store) -> None:
    async with tools_for(store) as tools:
        mine = await tools.call("list_orders", {}, session(CUSTOMER), key(2))
        theirs = await tools.call("list_orders", {}, session(STRANGER), key(3))

    assert {row["id"] for row in mine.structured["items"]} >= {"AB-10001", "AB-10003"}
    assert theirs.structured["items"] == [], "a stranger sees no orders at all"


# (why, the order, what the store must refuse it with)
REFUSALS = [
    ("a shipped order can no longer be cancelled", "AB-10001", "can no longer be cancelled"),
    ("a delivered order can no longer be cancelled", "AB-10003", "can no longer be cancelled"),
]


@pytest.mark.parametrize(("why", "order", "reason"), REFUSALS, ids=[r[0] for r in REFUSALS])
@pytest.mark.discharges("op:cancel_order", "P-CANCEL")
async def test_the_real_store_refuses_what_the_spec_says_it_must(
    store: Store, why: str, order: str, reason: str
) -> None:
    """The rule is written twice on purpose — here and in the spec the projected
    world reads — so that a disagreement between them is loud."""
    async with tools_for(store) as tools:
        result = await tools.call("cancel_order", {"id": order}, session(CUSTOMER), key(4))

    assert result.structured["allowed"] is False
    assert reason in result.structured["reason"]


@pytest.mark.discharges("op:open_return_request", "P-RETURN")
async def test_a_return_outside_the_window_is_refused_by_the_store(store: Store) -> None:
    """AB-10004 was delivered 31 days ago, and the store counts the days
    itself — from a delivery date Saleor does not have and the seed wrote."""
    async with tools_for(store) as tools:
        result = await tools.call(
            "open_return_request", {"id": "AB-10004"}, session(CUSTOMER), key(5)
        )
    assert result.structured["allowed"] is False
    assert "window closed" in result.structured["reason"]


@pytest.mark.discharges("op:open_return_request", "P-RETURN")
async def test_a_final_sale_return_is_refused_by_the_store(store: Store) -> None:
    async with tools_for(store) as tools:
        result = await tools.call(
            "open_return_request", {"id": "AB-10005"}, session(CUSTOMER), key(6)
        )
    assert result.structured["allowed"] is False
    assert "final sale" in result.structured["reason"]


@pytest.mark.discharges("AHC-0057", "AAC-0057", "op:issue_refund")
async def test_the_refund_tool_is_not_even_offered_to_a_customer(store: Store) -> None:
    """`refunds:write` is on no customer session, so the real store's refund
    tool is not on their surface at all — the first of the two controls."""
    async with tools_for(store) as tools:
        registry = await tools.list_tools(session(CUSTOMER))
    assert registry.get("issue_refund") is None
    assert registry.get("get_order") is not None


@pytest.mark.discharges("AHC-0057", "AAC-0057", "op:issue_refund")
async def test_a_refund_the_agent_elevated_itself_for_is_refused_by_the_store(
    store: Store,
) -> None:
    """The second control, and the one that matters: an identity that added the
    scope to itself in process still has to name an approval this store can
    check, and there is none. Nothing about the order changes."""
    elevated = session(CUSTOMER).model_copy(
        update={"scopes": ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE}}
    )
    async with tools_for(store) as tools:
        result = await tools.call("issue_refund", {"id": "AB-10002"}, elevated, key(7))

    assert result.is_error, result.text
    after = await store.get_order("AB-10002")
    assert after["status"] == "pending"
