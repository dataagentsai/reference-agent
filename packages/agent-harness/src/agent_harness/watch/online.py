"""The `online` position: a finished turn, read back from its trace, judged by
every evaluator `evaluators.yaml` places there.

The watch job's entry to the plug-in checks (Tier 2b). The same evaluators the
reply position runs inline, and any that are too slow for it, run here on what
the customer was actually sent, sampled per evaluator by the turn's trace id so
a rerun samples the same turns. A turn whose words were not captured has no
response, so an evaluator that needs one is `skip: missing response`, never a
pass.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_harness.contracts import ToolCall, ToolResult
from agent_harness.evals import EvalRequest, EvalResult, Meta, Response
from agent_harness.evals.plan import Plan
from agent_harness.watch.record import Turn


def request_of(turn: Turn, *, agent: str = "", version: str = "") -> EvalRequest:
    """The `EvalRequest` for one turn of the record (AHC-0114)."""
    calls = tuple(
        ToolCall(id=f"{turn.run_id}:{i}", name=t.name, arguments=dict(t.arguments or {}))
        for i, t in enumerate(turn.tools)
    )
    results = tuple(
        ToolResult(
            name=t.name,
            structured=t.result if isinstance(t.result, dict) else None,
            text="" if isinstance(t.result, dict) or t.result is None else str(t.result),
            is_error=t.outcome == "error",
        )
        for t in turn.tools
    )
    words = turn.captured and turn.reply is not None
    return EvalRequest(
        query=turn.input if turn.captured else None,
        messages=({"role": "user", "content": turn.input},) if words and turn.input else None,
        response=Response(text=turn.reply or "", tool_calls=calls, tool_results=results)
        if words
        else None,
        meta=Meta(
            agent=agent, version=version, trace=turn.trace_id, position="online", user=turn.user_id
        ),
    )


def judge(plan: Plan, turns: Iterable[Turn], *, agent: str = "") -> list[EvalResult]:
    """Every sampled online evaluator on every turn, synthetic ones left to the canary."""
    return [
        result
        for turn in turns
        if not turn.synthetic
        for result in plan.run_online(request_of(turn, agent=agent), key=turn.trace_id)
    ]


__all__ = ["judge", "request_of"]
