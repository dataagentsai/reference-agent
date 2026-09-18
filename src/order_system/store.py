"""Saleor as the order system: the agent's operations, against a real store.

T-017. The projected world answers these six operations from a YAML file; this
answers them from a store somebody else wrote, over that store's GraphQL API.
The agent cannot tell the difference — same names, same argument shapes, same
two error channels — and that is what makes the swap a test of the *spec* rather
than of the harness.

**The rules are restated here, not imported.** The AOAS says when a return may
be opened and when an order may still be cancelled; the projection reads that
spec, and so, in a deployment, would a real store's own policy engine. Writing
them again here is deliberate: two independent statements of one rule disagree
loudly, and a scenario that passes against the world and fails here has found
something. Sharing one implementation would hide exactly that.

**What Saleor does not have, the seed wrote down.** Saleor records that a
fulfilment was created, never that it arrived, and has no final-sale flag, so
`delivered_at` and `final_sale` are metadata (`deploy/saleor/seed.py`). A return
is a Saleor fulfilment in a returned state, and a refund with no payment gateway
behind it is a *granted* refund: its record of money owed back.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

RETURN_WINDOW_DAYS = 30
"""AOAS `open_return_request.preconditions`, restated. If this and the spec ever
disagree, a scenario says so, which is the reason it is written twice."""

CANCELLABLE = frozenset({"pending", "confirmed"})

DELIVERED_AT = "delivered_at"
FINAL_SALE = "final_sale"
WORLD_STATUS = "world_status"

RETURNED_FULFILMENTS = frozenset(
    {"RETURNED", "REFUNDED_AND_RETURNED", "WAITING_FOR_APPROVAL", "REPLACED"}
)
"""What Saleor calls a fulfilment that has gone back. `return_open` is the
question the agent asks, and the store's own record answers it (T-050)."""

ORDER_FIELDS = """
  id status externalReference customerNote
  total { gross { amount } }
  shippingAddress { streetAddress1 city countryArea postalCode }
  metadata { key value }
  fulfillments { status }
  user { externalReference }
  transactions { id }
  totalGrantedRefund { amount }
"""


class StoreUnavailable(RuntimeError):
    """The store could not be reached or would not answer. The caller turns this
    into the protocol error channel: nothing happened, and a retry may work."""


@dataclass
class Saleor:
    """The store's API, as these operations need it.

    A staff token, because this server *is* the store's own surface: it acts on
    the store's behalf and decides for itself whose call it is acting on, from
    the token and the approval the caller presents (`order_system.authoriser`).
    """

    url: str
    email: str
    password: str
    token: str | None = field(default=None, repr=False)

    async def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        """One call, signing in again once if the store's token has expired.

        Saleor's access tokens last minutes, and this held the first one for
        ever — so five minutes into a real session every call failed, and the
        agent handed a customer to a colleague over a cancellation the store
        would have allowed (found in the first person's session, T-017). The
        simulated shop never expires anything, which is why no test saw it.
        """
        if self.token is None:
            await asyncio.to_thread(self._sign_in)
        try:
            return await asyncio.to_thread(self._post, query, variables)
        except StoreUnavailable as exc:
            if not _expired(exc):
                raise
            self.token = None
            await asyncio.to_thread(self._sign_in)
            return await asyncio.to_thread(self._post, query, variables)

    def _sign_in(self) -> None:
        answer = self._post(
            """mutation($e: String!, $p: String!) {
                 tokenCreate(email: $e, password: $p) { token errors { message } } }""",
            {"e": self.email, "p": self.password},
        )
        token = answer["tokenCreate"]["token"]
        if not token:
            raise StoreUnavailable(f"{self.email} could not sign in to the store")
        self.token = token

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode()
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                answer = json.load(response)
        except (urllib.error.URLError, OSError) as exc:
            raise StoreUnavailable(f"the store did not answer: {exc}") from None
        if answer.get("errors"):
            raise StoreUnavailable(json.dumps(answer["errors"])[:300])
        return answer["data"]


SEPARATOR = "~"
"""Between a world id and the run its copy belongs to (`AB-10003~r7`, T-042).
Saleor cannot delete a completed order, so a scenario that spends one cannot be
undone; each shadow run gets a private copy instead, and nothing past this
module ever sees the suffix."""


def _unscoped(identifier: str) -> str:
    return identifier.split(SEPARATOR, 1)[0]


EXPIRED = ("ExpiredSignatureError", "Signature has expired", "JSONWebTokenExpired")


def _expired(exc: Exception) -> bool:
    """Whether the store refused the call only because our token is stale."""
    return any(marker in str(exc) for marker in EXPIRED)


def as_order(found: dict[str, Any], *, now: int) -> dict[str, Any]:
    """One Saleor order in the agent's vocabulary.

    The translation is the interesting part, and each line of it is a decision:
    `shipped` and `delivered` are one Saleor status told apart by a delivery
    date; `return_open` is whether any fulfilment has gone back; the address is
    one line because that is what the agent's tool takes.
    """
    meta = {row["key"]: row["value"] for row in found.get("metadata", [])}
    where = found.get("shippingAddress") or {}
    delivered_at = int(meta.get(DELIVERED_AT, 0))
    fulfilments = {row["status"] for row in found.get("fulfillments", [])}
    return {
        "id": _unscoped(found["externalReference"]),
        "customer_id": _unscoped((found.get("user") or {}).get("externalReference") or ""),
        "address": ", ".join(part for part in (where.get("streetAddress1"), _city(where)) if part),
        "total": int(float(found["total"]["gross"]["amount"])),
        "status": _status(found, delivered_at=delivered_at),
        "days_since_delivery": (now - delivered_at) // 86_400 if delivered_at else 0,
        "final_sale": meta.get(FINAL_SALE) == "true",
        "return_open": bool(fulfilments & RETURNED_FULFILMENTS),
        "note": found.get("customerNote") or "",
    }


def _city(where: dict[str, Any]) -> str:
    city = (where.get("city") or "").title()
    postal = where.get("postalCode") or ""
    return f"{city} {postal}".strip()


def _status(found: dict[str, Any], *, delivered_at: int) -> str:
    """Saleor's status in the agent's words.

    Saleor has no `delivered`, so the seed's delivery date is what tells it from
    `shipped` — and a store with a carrier integration would have a real event
    to read instead. `returned` and `refunded` come from the fulfilments and the
    charge status, which are the store's own records of both.
    """
    saleor = found["status"]
    fulfilments = {row["status"] for row in found.get("fulfillments", [])}
    if saleor == "CANCELED":
        return "cancelled"
    granted = float((found.get("totalGrantedRefund") or {}).get("amount") or 0)
    total = float(found["total"]["gross"]["amount"])
    if fulfilments & {"REFUNDED", "REFUNDED_AND_RETURNED"} or (total and granted >= total):
        return "refunded"
    if fulfilments & {"RETURNED"}:
        return "returned"
    if saleor in ("FULFILLED", "PARTIALLY_FULFILLED"):
        return "delivered" if delivered_at else "shipped"
    return "pending"


@dataclass
class Store:
    """The six operations, against Saleor. Each returns the shape the agent's
    tools already speak: a read says `found`, a write says `allowed` and why."""

    api: Saleor
    clock: Any = time.time
    namespace: str = ""
    """Which run's copy of the world this store answers for (T-042). Empty is
    the shared store a person talks to."""
    effects: list[tuple[str, str]] = field(default_factory=list)
    """Every write that landed, as `(operation, order)`: what a scenario's
    `effect` checks read, the same record the projected world keeps."""

    def now(self) -> int:
        return int(self.clock())

    def scoped(self, external: str) -> str:
        return f"{external}{SEPARATOR}{self.namespace}" if self.namespace else external

    def mine(self, raw: str | None) -> bool:
        """Whether a Saleor order belongs to this store's run."""
        if not raw:
            return False
        _, sep, space = raw.partition(SEPARATOR)
        return space == self.namespace if sep else not self.namespace

    async def order(self, external: str) -> dict[str, Any] | None:
        found = await self.api(
            f"query($r: String!) {{ order(externalReference: $r) {{ {ORDER_FIELDS} }} }}",
            r=self.scoped(external),
        )
        return found["order"]

    async def get_order(self, id: str) -> dict[str, Any]:
        found = await self.order(id)
        if found is None:
            return {"found": False, "id": id}
        return {"found": True, **as_order(found, now=self.now())}

    async def list_orders(self, customer_id: str) -> dict[str, Any]:
        """One customer's orders, newest first, from the customer's own record.

        `customer_id` is the verified session's, handed down by the server —
        never an argument the model wrote. It used to list the store's newest
        hundred orders and filter them, which stopped finding a customer's
        orders the moment the store held a hundred newer ones (T-042).
        """
        found = await self.api(
            "query($r: String!) { user(externalReference: $r) {"
            " orders(first: 100)"
            f" {{ edges {{ node {{ created {ORDER_FIELDS} }} }} }} }} }}",
            r=self.scoped(customer_id),
        )
        user = found["user"]
        now = self.now()
        # Newest first, sorted here: a customer's orders take no sort argument.
        edges = sorted(
            user["orders"]["edges"] if user else [],
            key=lambda edge: edge["node"]["created"],
            reverse=True,
        )
        rows = [
            as_order(edge["node"], now=now)
            for edge in edges
            if self.mine(edge["node"]["externalReference"])
        ]
        return {"found": True, "items": rows}

    async def cancel_order(self, id: str) -> dict[str, Any]:
        found = await self.order(id)
        if found is None:
            return {"allowed": False, "reason": "no such order", "id": id}
        row = as_order(found, now=self.now())
        if row["status"] not in CANCELLABLE:
            return self._refused(
                row, f"an order that is {row['status']} can no longer be cancelled"
            )
        await self.api(
            "mutation($id: ID!) { orderCancel(id: $id) { errors { field message } } }",
            id=found["id"],
        )
        return await self._after(id, "cancel_order")

    async def change_address(self, id: str, address: str) -> dict[str, Any]:
        found = await self.order(id)
        if found is None:
            return {"allowed": False, "reason": "no such order", "id": id}
        row = as_order(found, now=self.now())
        if row["status"] not in CANCELLABLE:
            return self._refused(
                row, f"the carrier owns the address once an order is {row['status']}"
            )
        street, _, rest = address.partition(", ")
        city, _, postal = rest.rpartition(" ")
        where = found.get("shippingAddress") or {}
        await self.api(
            """mutation($id: ID!, $i: OrderUpdateInput!) {
                 orderUpdate(id: $id, input: $i) { errors { field message } } }""",
            id=found["id"],
            i={
                "shippingAddress": {
                    "firstName": "Basant",
                    "lastName": "Kumar",
                    "streetAddress1": street or address,
                    "city": city or where.get("city") or "Bengaluru",
                    "countryArea": where.get("countryArea") or "Karnataka",
                    "postalCode": postal or where.get("postalCode") or "560001",
                    "country": "IN",
                }
            },
        )
        return await self._after(id, "change_address")

    async def open_return_request(self, id: str) -> dict[str, Any]:
        """The AOAS's four preconditions, checked where the state is.

        The last one is T-050's: a return already open makes a second request
        the same request, and the store's own fulfilments are what answer it.
        """
        found = await self.order(id)
        if found is None:
            return {"allowed": False, "reason": "no such order", "id": id}
        row = as_order(found, now=self.now())
        refusal = (
            f"no new return can be opened for this order: it is {row['status']}, "
            f"and a return is already open: {row['return_open']}"
        )
        if row["status"] != "delivered" or row["return_open"]:
            return self._refused(row, refusal)
        if row["days_since_delivery"] > RETURN_WINDOW_DAYS:
            return self._refused(
                row, f"the return window closed {row['days_since_delivery']} days after delivery"
            )
        if row["final_sale"]:
            return self._refused(row, "this order was a final sale and cannot be returned")

        # A return in Saleor is the fulfilment going back, so the lines it went
        # out on are what comes back.
        lines = await self.api(
            "query($id: ID!) { order(id: $id) { fulfillments { id lines { id quantity } } } }",
            id=found["id"],
        )
        fulfilment = (lines["order"]["fulfillments"] or [{}])[0]
        await self.api(
            """mutation($o: ID!, $i: OrderReturnProductsInput!) {
                 orderFulfillmentReturnProducts(order: $o, input: $i) {
                   errors { field message } } }""",
            o=found["id"],
            i={
                "fulfillmentLines": [
                    {"fulfillmentLineId": line["id"], "quantity": line["quantity"]}
                    for line in fulfilment.get("lines", [])
                ],
                "refund": False,
                "includeShippingCosts": False,
            },
        )
        return await self._after(id, "open_return_request")

    async def issue_refund(self, id: str) -> dict[str, Any]:
        """A refund, as a store with no payment gateway can mean it: a *granted*
        refund, which is its record that this money is owed back. What makes it
        happen is the same record a deployment's finance system would read."""
        found = await self.order(id)
        if found is None:
            return {"allowed": False, "reason": "no such order", "id": id}
        row = as_order(found, now=self.now())
        if row["status"] == "refunded":
            return self._refused(row, "this order has already been refunded")
        # Saleor grants a refund only against a payment it can name: money goes
        # back the way it came. An order nobody paid for cannot be refunded,
        # which the simulated world never had to say (T-042).
        payments = found.get("transactions") or []
        if not payments:
            return self._refused(row, "this order has no payment to refund")
        await self.api(
            """mutation($i: OrderGrantRefundCreateInput!, $o: ID!) {
                 orderGrantRefundCreate(id: $o, input: $i) {
                   errors { field message } } }""",
            o=found["id"],
            i={
                "amount": row["total"],
                "reason": "refund issued through the support agent",
                "transactionId": payments[0]["id"],
            },
        )
        return await self._after(id, "issue_refund")

    def _refused(self, row: dict[str, Any], reason: str) -> dict[str, Any]:
        return {"allowed": False, "reason": reason, **row}

    async def _after(self, external: str, operation: str) -> dict[str, Any]:
        """What the order is now, read back rather than assumed. A write that
        reported the state it intended would be the stale belief F-002 is about."""
        self.effects.append((operation, external))
        found = await self.order(external)
        assert found is not None
        return {"allowed": True, "reason": "allowed", **as_order(found, now=self.now())}


__all__ = [
    "CANCELLABLE",
    "RETURN_WINDOW_DAYS",
    "Saleor",
    "Store",
    "StoreUnavailable",
    "as_order",
]
