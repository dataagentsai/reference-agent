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

import json
from collections.abc import Awaitable, Callable, Mapping
from functools import partial

from agent_harness import flow as flw
from agent_harness import telemetry as tel
from agent_harness.contracts import (
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
    from agent_harness.contracts import SideEffectClass

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
    from agent_harness.contracts import ToolResult, UnknownTool

    local = local_tools.get(call.name)
    if local is not None:
        return await _local(local, call)

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


async def _local(local: LocalTool, call: ToolCall) -> ToolResult:
    """A harness-answered tool, recorded as an MCP one is: its arguments, its
    result and its outcome, so a reader of the record sees `request_payout`
    beside `get_claim` rather than a bare name (FINDINGS run 2, eval harness)."""
    with tel.span(
        "agent.tool.local",
        **{tel.GEN_AI_TOOL_NAME: call.name, tel.SIDE_EFFECT: local.spec.side_effect.value},
    ) as span:
        tel.set_payload(span, tel.TOOL_ARGUMENTS, json.dumps(call.arguments, sort_keys=True))
        outcome = "error"
        try:
            result = await local.handler(call.arguments)
            outcome = "error" if result.is_error else "ok"
            shown = result.structured if result.structured is not None else result.text
            tel.set_payload(span, tel.TOOL_RESULT, json.dumps(shown, default=str))
            return result
        finally:
            span.set_attribute(tel.TOOL_OUTCOME, outcome)


def _bounded(result: ToolResult, limit: int | None) -> ToolResult:
    """AAC-0105 for harness-answered tools, the same bound `GatedTools._bound`
    applies on the MCP path: measured on `for_context()`, cut with a mark that
    says so. A local tool's result reached context unbounded until F-5
    (clinical-data-review-agent)."""
    from agent_harness.tools import TRUNCATION_MARK

    rendered = result.for_context()
    if limit is None or len(rendered) <= limit:
        return result
    tel.counters.tool_truncated.add(1, {"tool": result.name})
    mark = TRUNCATION_MARK.format(total=len(rendered))
    cut = rendered[: max(0, limit - len(mark))] + mark
    return result.model_copy(update={"text": cut[:limit], "truncated": True})


def bounded_locals(
    local_tools: Mapping[str, LocalTool] | None, limit: int | None
) -> dict[str, LocalTool]:
    """Every local tool's handler, wrapped so its result enters context bounded."""

    def wrap(tool: LocalTool) -> LocalTool:
        async def handler(arguments: dict[str, object]) -> ToolResult:
            return _bounded(await tool.handler(arguments), limit)

        return LocalTool(spec=tool.spec, handler=handler)

    return {name: wrap(tool) for name, tool in (local_tools or {}).items()}


class _NoServer:
    """The tool server of a run that has none: lists nothing, answers nothing."""

    async def list_tools(self, identity: Identity) -> ToolRegistry:
        return ToolRegistry()

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey | None = None,
    ) -> ToolResult:
        from agent_harness.contracts import UnknownTool

        raise UnknownTool(name, available=())


def surface(tools: ToolClient | None, local_tools: Mapping[str, LocalTool] | None) -> ToolClient:
    """The run's tool server; none is needed when every tool is local (F-8)."""
    if tools is not None:
        return tools
    if not local_tools:
        raise ValueError("a run needs a tool server, local tools, or both")
    return _NoServer()


__all__ = ["bounded_locals", "dispatch", "surface"]
