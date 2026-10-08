"""Shadow mode (T-042): the same scenario, with the real store answering.

A scenario names a world, a customer and what it expects, and has only ever run
against the world projected from YAML. Here the store is Saleor, behind the
store's own MCP server, and the scenario's checks read the store instead of the
projection. Nothing about the scenario changes. A check that passes against the
world and fails here has found a difference, and the difference is the finding:
the spec described something the real store does not do, or the world file was
faithful about something that is not true of a store.

**Each run gets its own copy of the world.** Saleor cannot delete a completed
order, so a scenario that cancels one cannot be undone. The seed loads a private
copy under a run's namespace (`AB-10003~r7` in Saleor) and the store server
strips it, so the agent, the customer and the checks all still say `AB-10003`.

**Not every scenario can be shadowed yet**, and the ones that cannot say why
(`unshadowable`): a fault injected into the projected world has no counterpart
in a real store until AgentTwin can perturb one, and a real store cannot be
told that days have passed.
"""

from __future__ import annotations

import copy
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from agenttwin import Clock, Live, Subject, load, load_scenario
from deploy.saleor import seed as seeding

from agent_harness import identity as ident
from agent_harness.requests import InMemoryRequests
from evals import issuer as issuing
from evals.simulation import subject_for
from order_system import server as store_server
from order_system.store import ORDER_FIELDS, Saleor, Store, StoreUnavailable, _expired, as_order
from support_agent.contracts import Identity, LLMClient
from support_agent.tools import connect

URL = os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")
EMAIL = os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only")


def unshadowable(path: Path) -> str | None:
    """Why this scenario cannot run against a real store yet, or `None`."""
    scenario = load_scenario(path)
    if scenario.perturbations:
        kinds = sorted({p.kind for p in scenario.perturbations})
        if any(not kind.startswith("provider_") for kind in kinds):
            named = ", ".join(kinds)
            return f"a fault injected into the world ({named}) has no real-store counterpart yet"
    if scenario.step_days:
        return "a real store cannot be told that days have passed"
    return None


def reachable() -> str | None:
    """Why no real store can be used, or `None` when one answers."""
    try:
        seeding.login(URL, EMAIL, PASSWORD)
    except (OSError, seeding.SaleorError) as exc:
        return f"no Saleor at {URL}: {exc}"
    return None


class ShadowLive:
    """The world as the scenario's checks read it, answered by Saleor.

    The same four things the projected world answers — a row by key, a snapshot
    of every row, the effects that landed, and the world's own definition — so
    the checks run unchanged. Reads are synchronous because the checks are.
    """

    def __init__(self, projected: Live, store: Store) -> None:
        self.world = projected.world
        self._customers = copy.deepcopy(projected.rows.get("customer", {}))
        self.store = store
        self.effects = store.effects

    @property
    def rows(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {"customer": copy.deepcopy(self._customers), "order": self._orders()}

    def get(self, entity: str, key: str) -> dict[str, Any] | None:
        return self.rows.get(entity, {}).get(key)

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        return self.rows

    def count(self, action: str) -> int:
        return sum(1 for done, _ in self.effects if done == action)

    def advance(self, days: int) -> tuple[str, ...]:
        raise NotImplementedError("a real store cannot be told that days have passed")

    def _orders(self) -> dict[str, dict[str, Any]]:
        """Every order of this run's customers, through each customer's record."""
        now, rows = self.store.now(), {}
        query = (
            "query($r: String!) { user(externalReference: $r) {"
            f" orders(first: 100) {{ edges {{ node {{ {ORDER_FIELDS} }} }} }} }} }}"
        )
        for customer in self._customers:
            user = self._call(query, r=self.store.scoped(customer))["user"]
            for edge in user["orders"]["edges"] if user else []:
                if self.store.mine(edge["node"]["externalReference"]):
                    row = as_order(edge["node"], now=now)
                    rows[row["id"]] = row
        return rows

    def _call(self, query: str, **variables: Any) -> dict[str, Any]:
        api = self.store.api
        if api.token is None:
            api._sign_in()
        try:
            return api._post(query, variables)
        except StoreUnavailable as exc:
            if not _expired(exc):
                raise
            api.token = None
            api._sign_in()
            return api._post(query, variables)


def session(customer_id: str) -> Identity:
    """A customer's session as the chat edge hands it on: signed, because a
    real store verifies it rather than taking the customer id on trust."""
    return ident.verify(issuing.mint(customer_id), issuer=issuing.issuer()).as_customer()


async def signed(customer_id: str) -> Identity:
    """The approvals worker's login, here the customer's own signed session."""
    return session(customer_id)


@asynccontextmanager
async def shadowed(
    path: Path, *, llm: LLMClient, clock: Clock, **options: Any
) -> AsyncIterator[tuple[Subject, ShadowLive]]:
    """The agent wired to a private copy of the scenario's world in Saleor, and
    the world the scenario's checks will read."""
    scenario = load_scenario(path)
    world_path = path.parent / scenario.world
    projected = Live.start(load(world_path))
    namespace = f"r{uuid.uuid4().hex[:6]}"
    records = yaml.safe_load(world_path.read_text())
    seeding.seed(
        seeding.login(URL, EMAIL, PASSWORD), records, now=int(time.time()), namespace=namespace
    )
    store = Store(Saleor(URL, EMAIL, PASSWORD), namespace=namespace)

    def shop(waits: Any) -> Any:
        server = store_server.build(
            store,
            issuer=issuing.issuer(audience="order-system"),
            clock=clock,
            approvals=waits.approvals,
        )
        return connect(server, requests=InMemoryRequests(), exchange=issuing.LocalExchange())

    async with subject_for(
        projected, llm=llm, clock=clock, shop=shop, who=session, acting_for=signed, **options
    ) as subject:
        yield subject, ShadowLive(projected, store)


__all__ = ["ShadowLive", "reachable", "session", "shadowed", "unshadowable"]
