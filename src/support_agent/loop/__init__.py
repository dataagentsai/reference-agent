"""When the system stops.

L4 · P4. The ReAct loop, hand-written, because a framework owning this layer is
a framework hiding the layer the catalogs exist to expose.

P4 is the only position that can see a *trajectory* — that these fourteen calls
are one runaway task rather than fourteen tasks. Everything enforced here needs
that view, and nothing else has it.

Three terminations that are not "the model said it was done":

**Step budget.** AAC-0055 — hard termination under every condition. An
unexplained stop is indistinguishable from a hang, so the reason is always
recorded.

**Oscillation.** A→B→A→B inside the budget. AAC-0055 catches hard
non-termination and AAC-0054 scores path efficiency against a reference, but
neither catches a loop that repeats itself and then stops in time. That gap is
recorded as G2 against the catalog; this is the implementation that will justify
filling it.

**Provider failure.** A declared degradation path (AAC-0009), returned as a typed
`Failed` rather than escaping as a stack trace.

**Cost ceiling.** Checked between calls, because you cannot un-spend one — so a
single call may overshoot, bounded by `max_output_tokens`. Attribution happens
at P3 where the call is made; the ceiling is here because only P4 can see the
whole task.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from support_agent import context as ctx
from support_agent import telemetry as tel
from support_agent.config import Budgets
from support_agent.contracts import (
    Completed,
    Failed,
    IdempotencyKey,
    Identity,
    LLMClient,
    Message,
    ModelRequest,
    ModelUnavailable,
    RunId,
    TerminationReason,
    ToolClient,
    ToolUnavailable,
    TurnResult,
    Usage,
    new_run_id,
)
from support_agent.cost import Meter


@dataclass
class Trace:
    """What the loop accumulated. Returned alongside the result so a caller can
    assert on the trajectory without reading spans."""

    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    spend_usd: float = 0.0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    termination: TerminationReason = TerminationReason.GOAL_REACHED

    def signature_counts(self) -> Counter[tuple[str, str]]:
        return Counter(self.tool_calls)


def _signature(name: str, arguments: dict[str, object]) -> tuple[str, str]:
    """A call's identity for oscillation purposes: the tool and its arguments.

    Sorted keys, because `{"a":1,"b":2}` and `{"b":2,"a":1}` are the same call
    and a detector that thinks otherwise never fires.
    """
    return name, json.dumps(arguments, sort_keys=True, default=str)


async def run(
    goal: str,
    *,
    identity: Identity,
    llm: LLMClient,
    tools: ToolClient,
    system_prompt: str,
    budgets: Budgets | None = None,
    run_id: RunId | None = None,
    history: tuple[Message, ...] = (),
    oscillation_threshold: int = 3,
    meter: Meter | None = None,
) -> tuple[TurnResult, Trace]:
    budgets = budgets or Budgets()
    run_id = run_id or new_run_id()
    trace = Trace()
    messages: list[Message] = [*history, ctx.user_message(goal)]

    with tel.span(
        "agent.run", **{tel.RUN_ID: run_id, tel.TENANT: identity.customer_id}
    ) as run_span:
        try:
            registry = await tools.list_tools(identity)
        except ToolUnavailable as exc:
            return _failed(run_span, trace, "I cannot reach our order system right now.", str(exc))

        tool_defs = ctx.model_tools(registry)

        for step in range(budgets.max_steps):
            trace.steps = step + 1
            with tel.span("agent.step", **{tel.STEP: step, tel.RUN_ID: run_id}):
                request = ModelRequest(
                    messages=ctx.assemble(system=system_prompt, history=messages),
                    tools=tool_defs,
                    max_tokens=budgets.max_output_tokens,
                )
                try:
                    response = await llm.complete(request)
                except ModelUnavailable as exc:
                    return _failed(
                        run_span, trace, "I am having trouble answering right now.", str(exc)
                    )

                trace.usage = Usage(
                    input_tokens=trace.usage.input_tokens + response.usage.input_tokens,
                    output_tokens=trace.usage.output_tokens + response.usage.output_tokens,
                )
                if meter is not None:
                    call_cost = meter.record(response.usage)
                    trace.spend_usd = meter.as_usd()
                    run_span.set_attribute(tel.COST_CALL_USD, float(call_cost))
                    run_span.set_attribute(tel.COST_USD, trace.spend_usd)

                if not response.wants_tools:
                    return _completed(run_span, trace, response.text)

                if meter is not None and meter.exceeded:
                    return _stopped(
                        run_span,
                        trace,
                        TerminationReason.COST_CEILING_REACHED,
                        "I have not been able to resolve this — let me pass you to a colleague.",
                    )

                messages.append(Message(role="assistant", content=response.text or ""))

                for call in response.tool_calls:
                    signature = _signature(call.name, call.arguments)
                    trace.tool_calls.append(signature)

                    if trace.signature_counts()[signature] >= oscillation_threshold:
                        return _stopped(
                            run_span,
                            trace,
                            TerminationReason.OSCILLATION_DETECTED,
                            "I am going round in circles on this — let me pass you to a colleague.",
                        )

                    key = IdempotencyKey(run_id=run_id, step=step, iteration=len(trace.tool_calls))
                    result = await _invoke(tools, call, identity, key)
                    messages.append(ctx.tool_message(result, tool_call_id=call.id))

        return _stopped(
            run_span,
            trace,
            TerminationReason.STEP_BUDGET_EXHAUSTED,
            "I have not been able to resolve this — let me pass you to a colleague.",
        )


async def _invoke(tools: ToolClient, call, identity: Identity, key: IdempotencyKey):
    """Every failure is reported back to the model rather than raised.

    A tool that does not exist, or arguments that do not validate, are things the
    model can correct on the next step — AAC-0051 and AAC-0052 are about recovery,
    not about crashing. What must never be swallowed is an *effect*, and there is
    none here: nothing ran.
    """
    from support_agent.contracts import ToolResult, UnknownTool

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


def _completed(span, trace: Trace, text: str) -> tuple[TurnResult, Trace]:
    trace.termination = TerminationReason.GOAL_REACHED
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Completed(reply=text), trace


def _stopped(
    span, trace: Trace, reason: TerminationReason, message: str
) -> tuple[TurnResult, Trace]:
    trace.termination = reason
    span.set_attribute(tel.TERMINATION, reason.value)
    return Completed(reply=message, termination=reason), trace


def _failed(span, trace: Trace, customer_message: str, detail: str) -> tuple[TurnResult, Trace]:
    trace.termination = TerminationReason.UNRECOVERABLE_ERROR
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Failed(customer_message=customer_message, detail=detail), trace


__all__ = ["Trace", "run"]
