"""Running the calls a step planned.

One responsibility: getting from a list of planned calls to a list of results,
with the concurrency rule the harness declares rather than the one the model
asked for — reads may overlap, everything else runs in the order requested
(AHC-0104). Every failure comes back as a result: a tool that does not exist, or
arguments that do not validate, are things the model can correct on the next
step. What must never be swallowed is an *effect*, and there is none here —
nothing ran.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial

from agent_harness import flow as flw
from agent_harness import telemetry as tel
from support_agent.contracts import (
    IdempotencyKey,
    Identity,
    LocalTool,
    ToolCall,
    ToolClient,
    ToolRegistry,
    ToolResult,
    ToolUnavailable,
)


async def dispatch(
    tools: ToolClient,
    planned: list[tuple[ToolCall, IdempotencyKey]],
    identity: Identity,
    local_tools: Mapping[str, LocalTool],
    registry: ToolRegistry,
    fan_out: int,
) -> list[ToolResult]:
    """Reads concurrently, everything else in the order the model asked.

    A read that fails costs a retry. A write that fails halfway through a
    parallel batch costs a reconciliation, in an order that depended on
    scheduling — and a compensating path is far easier to reason about when the
    writes happened one at a time.
    """
    from support_agent.contracts import SideEffectClass

    def is_read(call: ToolCall) -> bool:
        spec = registry.get(call.name)
        return spec is not None and spec.side_effect is SideEffectClass.READ

    if len(planned) > 1 and all(is_read(call) for call, _ in planned):
        thunks: list[Callable[[], Awaitable[ToolResult]]] = [
            partial(_invoke, tools, call, identity, key, local_tools) for call, key in planned
        ]
        return await flw.gather_bounded(thunks, limit=fan_out)

    return [await _invoke(tools, call, identity, key, local_tools) for call, key in planned]


async def _invoke(
    tools: ToolClient,
    call: ToolCall,
    identity: Identity,
    key: IdempotencyKey,
    local_tools: Mapping[str, LocalTool],
) -> ToolResult:
    """Every failure is reported back to the model rather than raised.

    A tool that does not exist, or arguments that do not validate, are things the
    model can correct on the next step — AAC-0051 and AAC-0052 are about recovery,
    not about crashing. What must never be swallowed is an *effect*, and there is
    none here: nothing ran.
    """
    from support_agent.contracts import ToolResult, UnknownTool

    local = local_tools.get(call.name)
    if local is not None:
        with tel.span("agent.tool.local", **{tel.GEN_AI_TOOL_NAME: call.name}):
            return await local.handler(call.arguments)

    try:
        return await tools.call(call.name, call.arguments, identity, key)
    except UnknownTool as exc:
        return ToolResult(
            name=call.name,
            text=f"no such tool; available: {', '.join(exc.available)}",
            is_error=True,
            error_channel="protocol",
        )
    except ToolUnavailable as exc:
        return ToolResult(name=call.name, text=str(exc), is_error=True, error_channel="protocol")
    except Exception as exc:  # schema validation and anything else recoverable
        return ToolResult(
            name=call.name,
            text=f"invalid call: {exc}",
            is_error=True,
            error_channel="execution",
        )


__all__ = ["dispatch"]
