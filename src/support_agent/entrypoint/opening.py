"""What a customer is shown when they open a conversation: P-OPEN (T-001).

Before this, the chat opened empty. The customer had to supply an order number
they rarely have to hand, so the first turns of every real conversation went on
finding the order, and work already in flight (a refund waiting on a colleague,
a case with the desk) stayed invisible until they happened to ask.

Opening is the cheapest turn there is, and it is built to stay that way: one
read of the customer's own orders through the order system (`list_orders`,
scoped by the session there, not here), a filter over the agent's own queues,
and a string. **No model call**, so an abandoned open, which is most opens, costs
a read and nothing else.
"""

from __future__ import annotations

from typing import Any

from support_agent import telemetry as tel
from support_agent.contracts import (
    Approvals,
    EscalationStore,
    IdempotencyKey,
    Identity,
    RunId,
    ToolClient,
)

LIST_ORDERS = "list_orders"
SHOWN = 5
"""Orders listed by number. Past that the customer is told how many more, which
is enough to ask about any of them by number."""


async def opening(
    identity: Identity,
    *,
    tools: ToolClient,
    approvals: Approvals | None,
    escalations: EscalationStore | None,
) -> str:
    with tel.span("agent.opening", **{tel.TENANT: identity.customer_id}) as span:
        orders = await _orders(tools, identity)
        waiting = await _in_flight(identity.customer_id, approvals, escalations)
        span.set_attribute("agent.opening.orders", -1 if orders is None else len(orders))
        span.set_attribute("agent.opening.in_flight", len(waiting))
    return _words(orders, waiting)


async def _orders(tools: ToolClient, identity: Identity) -> list[dict[str, Any]] | None:
    """The customer's orders as the order system sees them, or `None` if it would
    not say. A read, so the key is never used for deduplication."""
    key = IdempotencyKey(run_id=RunId("opening"), step=0, iteration=0)
    result = await tools.call(LIST_ORDERS, {}, identity, key)
    structured = None if result.is_error else result.structured
    items = structured.get("items") if isinstance(structured, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None


async def _in_flight(
    customer_id: str, approvals: Approvals | None, escalations: EscalationStore | None
) -> list[str]:
    waiting: list[str] = []
    pending = await approvals.pending() if approvals is not None else ()
    for approval in pending:
        if approval.customer_id == customer_id:
            about = approval.args.get("order_id") or "an order"
            action = approval.action.replace("_", " ")
            waiting.append(f"{action} for {about}, waiting for a colleague to approve")
    queued = await escalations.pending() if escalations is not None else ()
    for escalation in queued:
        if escalation.customer_id == customer_id:
            waiting.append(f"{escalation.id}, with a colleague")
    return waiting


def _words(orders: list[dict[str, Any]] | None, waiting: list[str]) -> str:
    lines = ["Hello."]
    if orders is None:
        lines.append(
            "I cannot see your orders right now, but you can still ask me about one by its number."
        )
    elif not orders:
        lines.append("You have no orders with us yet.")
    else:
        lines.append("Your orders:")
        lines.extend(f"- {o.get('id')}: {o.get('status')}" for o in orders[:SHOWN])
        if len(orders) > SHOWN:
            lines.append(f"- and {len(orders) - SHOWN} more")
    if waiting:
        lines.append("Already in hand:")
        lines.extend(f"- {w}" for w in waiting)
    lines.append("Which one can I help with?" if orders else "What can I help with?")
    return "\n".join(lines)


__all__ = ["LIST_ORDERS", "opening"]
