"""The policy positions around a model call and a tool call.

One responsibility: reach the rules. *Which* rules run is this agent's
configuration and lives in `policy`; that they are reached at all is the
harness's job, and it was not being done — three of the five positions were
declared, accepted rules, and called nothing (F-027).

What a block means differs by position, and that is the whole of the design:

    before the model   end the turn — there is no lesser thing to do, and
                       refusing here is the last refusal that costs nothing
    before a tool      answer the model instead of calling — the action did not
                       happen, and the model may choose again (AAC-0051)
    after a tool       replace what came back — the effect has happened and this
                       cannot undo it, only refuse to carry it into context
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import Span

from support_agent import policy as pol
from support_agent.contracts import IdempotencyKey, Identity, ToolCall, ToolResult


@dataclass(frozen=True)
class Screen:
    """Bound to one run: whose identity, which rules, and where to record."""

    identity: Identity
    rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None
    span: Span

    def at(
        self, position: pol.Position, results: tuple[ToolResult, ...], **fields: Any
    ) -> pol.Verdict:
        configured = None if self.rules is None else self.rules.get(position)
        verdict = pol.enforce(
            pol.Context(position=position, identity=self.identity, tool_results=results, **fields),
            configured,
        )
        if verdict.blocked:
            self.span.set_attribute("agent.policy.blocked_by", verdict.rule)
        return verdict

    def permitted(
        self, planned: list[tuple[ToolCall, IdempotencyKey]], results: tuple[ToolResult, ...]
    ) -> dict[str, ToolResult]:
        """The calls a rule refused, as the answers the model will be given.

        Screened before anything runs, so a refused call costs nothing and the
        calls beside it are unaffected.
        """
        refused: dict[str, ToolResult] = {}
        for call, _ in planned:
            verdict = self.at(
                pol.Position.PRE_TOOL,
                results,
                tool_name=call.name,
                arguments=dict(call.arguments),
            )
            if verdict.blocked:
                refused[call.id] = ToolResult(
                    name=call.name,
                    text=pol.BLOCKED_CALL.format(reason=verdict.reason or verdict.rule),
                    is_error=True,
                    error_channel="protocol",
                )
        return refused

    def admitted(
        self, call: ToolCall, result: ToolResult, results: tuple[ToolResult, ...]
    ) -> ToolResult:
        """The result as it may enter context, or a stand-in saying it may not."""
        verdict = self.at(
            pol.Position.POST_TOOL,
            results,
            tool_name=call.name,
            arguments=dict(call.arguments),
            result=result,
            text=result.for_context(),
        )
        if not verdict.blocked:
            return result
        return ToolResult(
            name=result.name,
            text=pol.BLOCKED_RESULT.format(rule=verdict.rule),
            is_error=True,
            error_channel="protocol",
        )


__all__ = ["Screen"]
