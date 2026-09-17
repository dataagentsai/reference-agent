"""The agent, end to end, against the real store (T-017).

    docker compose --profile store up -d saleor
    uv run python deploy/saleor/seed.py

`Agent.handle()` with a scripted model and the store's own MCP server behind it:
the whole path a customer's message takes, with nothing simulated but the model.
Skipped when no Saleor answers.

These write to a store other tests read, so each one is written to be true on a
second run — the store keeps what happened, which is the point of it.
"""

from __future__ import annotations

import os
import time
import urllib.error

import pytest
from deploy.saleor import seed as seeding
from evals import durable
from evals import issuer as issuing

from order_system import server as store_server
from order_system.store import Saleor, Store
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

URL = os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")
CUSTOMER = "C-1042"


def store() -> Store:
    api = Saleor(
        URL,
        os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com"),
        os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only"),
    )
    try:
        seeding.login(URL, api.email, api.password)
    except (urllib.error.URLError, seeding.SaleorError, OSError) as exc:
        pytest.skip(f"no Saleor at {URL}: {exc}")
    return Store(api)


def who() -> Identity:
    """A verified session, as the chat edge hands it on."""
    return ident.verify(issuing.mint(CUSTOMER), issuer=issuing.issuer()).as_customer()


def calls(name: str, **arguments: object) -> ModelResponse:
    return ModelResponse(tool_calls=(ToolCall(id="c1", name=name, arguments=arguments),))


def talking(shop: Store, llm: ScriptedClient):
    """The agent, with the real store behind its tools and nothing else changed."""
    server = store_server.build(
        shop,
        issuer=issuing.issuer(audience="order-system"),
        clock=lambda: int(time.time()),
        approvals=durable.Remembered(),
    )
    return connect(server, ledger=InMemoryLedger(), exchange=issuing.LocalExchange())


@pytest.mark.discharges("P-OPEN", "op:list_orders", "P-OWNERSHIP")
async def test_opening_shows_the_customer_their_real_orders() -> None:
    """T-001 against a store somebody else wrote: the opening costs a read and
    a string, and the orders in it are real ones."""
    shop = store()
    async with talking(shop, ScriptedClient([])) as tools:
        agent = ep.build(llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore())
        opening = await agent.opening(who())

    assert "AB-" in opening, opening
    assert "Which one can I help with" in opening


@pytest.mark.discharges("op:cancel_order", "P-CANCEL", "AAC-0110")
async def test_the_store_refuses_a_cancellation_and_the_agent_says_so() -> None:
    """AB-10001 has shipped. The refusal is the store's, and what the customer
    is told comes from that refusal rather than from what the model expected."""
    shop = store()
    llm = ScriptedClient(
        [
            calls("cancel_order", id="AB-10001"),
            ModelResponse(text="That order has already shipped, so it can no longer be cancelled."),
        ]
    )
    async with talking(shop, llm) as tools:
        agent = ep.build(llm=llm, tools=tools, store=InMemoryCheckpointStore())
        result, _ = await agent.handle("please cancel AB-10001", identity=who())

    assert "no longer be cancelled" in result.reply
    after = await shop.get_order("AB-10001")
    assert after["status"] == "shipped", "a refusal that still changed the order is not one"


async def returns_on(shop: Store, external: str) -> int:
    """How many fulfilments of this order have gone back, as the store holds it."""
    found = await shop.order(external)
    assert found is not None
    return len([f for f in found["fulfillments"] if f["status"] != "FULFILLED"])


@pytest.mark.discharges("op:open_return_request", "P-RETURN", "AHC-0074")
async def test_a_second_return_request_is_refused_by_the_store_itself() -> None:
    """T-050, against a real store.

    A lost reply made the model ask again and two returns were opened, because
    a return was prose rather than state. Saleor holds the state itself — a
    fulfilment that has gone back — so the second request is refused by the
    store, whatever the agent believes.
    """
    shop = store()
    llm = ScriptedClient([calls("open_return_request", id="AB-10003"), ModelResponse(text="Done.")])
    async with talking(shop, llm) as tools:
        agent = ep.build(llm=llm, tools=tools, store=InMemoryCheckpointStore())
        before = await returns_on(shop, "AB-10003")
        await agent.handle("I want to return AB-10003", identity=who())
        opened = await shop.get_order("AB-10003")
        after_first = await returns_on(shop, "AB-10003")

        # A second run, a second conversation: nothing the agent remembers can
        # be what refuses this one.
        agent.llm = ScriptedClient(
            [calls("open_return_request", id="AB-10003"), ModelResponse(text="Already open.")]
        )
        await agent.handle("I want to return AB-10003", identity=who())
        after_second = await returns_on(shop, "AB-10003")

    assert opened["return_open"] is True, "the store holds that a return is open"
    assert after_first == max(before, 1), "the first request opened one return"
    assert after_second == after_first, "and the second opened none"
