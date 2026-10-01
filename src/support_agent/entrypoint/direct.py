"""The deterministic route: questions answered without a model call.

A registry keyed by the handler name the router chose. Adding a deterministic
answer adds an entry here; nothing that dispatches changes. Every handler
returns a typed result on every path — the output contract holds on the route
where nobody expects a surprise, which is exactly why one once escaped (F-005).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from support_agent import context as ctx
from support_agent import telemetry as tel
from support_agent.contracts import (
    Completed,
    Direct,
    Failed,
    IdempotencyKey,
    Identity,
    RunId,
    ToolClient,
    ToolUnavailable,
    TurnResult,
    bind_arguments,
)

LOOKUP_TOOL = "get_order"

STATUS_REPLY = (
    "Order {{ order_id }} is currently {{ status }}."
    "{% if status == 'shipped' %} It is on its way.{% endif %}"
)


class DirectHandler(Protocol):
    async def __call__(
        self, decision: Direct, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> TurnResult: ...


NOT_FOUND_REPLY = "I could not find order {{ order_id }} on your account."
"""What `found: false` is told. The order system answers it alike for an order
that does not exist and for one that is somebody else's (F-016), so the reply
names neither case — it says the one thing both share."""


async def _order(
    decision: Direct, identity: Identity, run_id: RunId, tools: ToolClient
) -> dict[str, object] | Completed | Failed:
    """The order the router found, from the order system — or an answer that it
    was not found, or a typed failure.

    `found: false` is a successful call that says the order is absent (AHC-0086:
    absence is its own state, not a field left empty). Read as an order, it
    became *"Order AB-10003 is currently unknown."* and *"There is no refund on
    order AB-10003."* — both describing an order the system had just said was
    not there (F-062).

    The tool's argument name is read from its declared schema rather than
    assumed: a hard-coded `order_id` once bound this route to one tool signature,
    and a world whose key field is `id` made it raise rather than degrade (F-005).
    """
    key = IdempotencyKey(run_id=run_id, step=0, iteration=0)
    try:
        registry = await tools.list_tools(identity)
        spec = registry.get(LOOKUP_TOOL)
        if spec is None:
            return Failed(
                customer_message="I cannot look that up right now.",
                detail=f"{LOOKUP_TOOL} is not on this identity's surface",
            )
        result = await tools.call(LOOKUP_TOOL, bind_arguments(spec, decision.args), identity, key)
    except ToolUnavailable as exc:
        return Failed(
            customer_message="I cannot reach our order system right now.", detail=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 — the contract holds here too
        return Failed(
            customer_message="I could not look that up.", detail=f"{type(exc).__name__}: {exc}"
        )
    if result.is_error or not isinstance(result.structured, dict):
        return Failed(
            customer_message="I could not find that order.",
            detail=result.text or "no structured content",
        )
    if result.structured.get("found") is False:
        return Completed(
            reply=ctx.render(NOT_FOUND_REPLY, order_id=decision.args.get("order_id", ""))
        )
    return result.structured


def _order_id(order: dict[str, object]) -> object:
    return order.get("order_id") or order.get("id", "")


async def order_status(
    decision: Direct, identity: Identity, run_id: RunId, tools: ToolClient
) -> TurnResult:
    """The order's current status, from the order system, rendered by template."""
    order = await _order(decision, identity, run_id, tools)
    if not isinstance(order, dict):
        return order
    return Completed(
        reply=ctx.render(
            STATUS_REPLY, order_id=_order_id(order), status=order.get("status", "unknown")
        )
    )


REFUND_REPLIES: Mapping[str, str] = {
    "refunded": (
        "A refund has been issued for order {{ order_id }}, to the original payment method."
    ),
    "returned": (
        "Your return for order {{ order_id }} has arrived, and the refund is being processed."
    ),
}
NO_REFUND_REPLY = "There is no refund on order {{ order_id }}."


async def refund_status(
    decision: Direct, identity: Identity, run_id: RunId, tools: ToolClient
) -> TurnResult:
    """AOAS `P-REFUND-STATUS`: the refund state, read from the order's status.

    The order system holds no refund record of its own, so the status is the
    whole of what can be said — and the replies say no more than it supports:
    never a date, never an amount. Before this handler existed, a refund-status
    question was answered with the order's status (F-018).
    """
    order = await _order(decision, identity, run_id, tools)
    if not isinstance(order, dict):
        return order
    template = REFUND_REPLIES.get(str(order.get("status")), NO_REFUND_REPLY)
    return Completed(reply=ctx.render(template, order_id=_order_id(order)))


HANDLERS: Mapping[str, DirectHandler] = {
    "order_status": order_status,
    "refund_status": refund_status,
}


async def answer(
    decision: Direct,
    identity: Identity,
    run_id: RunId,
    tools: ToolClient,
    handlers: Mapping[str, DirectHandler] = HANDLERS,
) -> TurnResult:
    """Dispatch to the handler the router named. No model call, and the trace says so."""
    with tel.span("agent.direct", **{"agent.handler": decision.handler}):
        handler = handlers.get(decision.handler)
        if handler is None:
            return Failed(
                customer_message="I cannot look that up right now.",
                detail=f"no deterministic handler named {decision.handler!r}",
            )
        return await handler(decision, identity, run_id, tools)


__all__ = [
    "HANDLERS",
    "LOOKUP_TOOL",
    "NOT_FOUND_REPLY",
    "NO_REFUND_REPLY",
    "REFUND_REPLIES",
    "STATUS_REPLY",
    "DirectHandler",
    "answer",
    "order_status",
    "refund_status",
]
