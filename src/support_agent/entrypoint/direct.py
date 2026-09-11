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


async def order_status(
    decision: Direct, identity: Identity, run_id: RunId, tools: ToolClient
) -> TurnResult:
    """The order's current status, from the order system, rendered by template.

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
    return Completed(
        reply=ctx.render(
            STATUS_REPLY,
            order_id=result.structured.get("order_id") or result.structured.get("id", ""),
            status=result.structured.get("status", "unknown"),
        )
    )


HANDLERS: Mapping[str, DirectHandler] = {
    "order_status": order_status,
    # F-018, made visible. The router routes refund-status questions here and no
    # refund-status handler exists yet, so the order's status answers them — as
    # it always silently did. The fix is a real handler, in its own commit.
    "refund_status": order_status,
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


__all__ = ["HANDLERS", "LOOKUP_TOOL", "STATUS_REPLY", "DirectHandler", "answer", "order_status"]
