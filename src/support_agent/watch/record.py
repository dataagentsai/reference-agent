"""A turn, rebuilt from its trace: the evaluation record AHC-0114 declares.

Two sources, one shape. `from_spans` reads finished spans in process — what a
test holds — and `from_observations` reads what Langfuse returns for the same
trace, where every span attribute arrives as `metadata["attributes.<name>"]`.
Both become `Node`s first, so the turn is assembled by one function and a rule
cannot pass in a test and fail in production because the two readers disagreed.

Only this agent's spans are read (`scope.name == support_agent`): the MCP SDK
instruments itself and names its spans after the tool too, and counting its
`get_order` beside ours would count every call twice.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from support_agent import telemetry as tel


@dataclass(frozen=True)
class Node:
    """One span, whichever store it came from."""

    trace_id: str
    name: str
    attributes: Mapping[str, Any]
    start: float
    end: float


@dataclass(frozen=True)
class ToolUse:
    name: str
    side_effect: str
    outcome: str
    arguments: Mapping[str, Any] | None
    result: Any
    duration_s: float
    truncated: bool = False


@dataclass(frozen=True)
class Turn:
    """What a rule reads. Every field is from the record, never inferred here."""

    trace_id: str
    run_id: str
    session_id: str
    user_id: str
    started: float
    duration_s: float
    synthetic: bool
    captured: bool
    result: str
    rule_id: str
    reply_redacted: bool
    input: str | None
    reply: str | None
    route: str
    termination: str
    cost_usd: float
    model_calls: int
    malformed: int
    unbacked_promise: bool
    tools: tuple[ToolUse, ...]
    input_tokens: int = 0
    """Input tokens across the turn's model calls (AACP-0003): what this turn
    paid to resend the conversation so far."""
    models: tuple[tuple[str, str], ...] = ()
    """Each model call's (requested, served) model names, as the provider wrote
    them (AACP-0054). `checks.model_name` is the one form they are compared in."""


@dataclass(frozen=True)
class Feedback:
    """A customer's verdict on a conversation (AHC-0112)."""

    session_id: str
    user_id: str
    value: str
    at: float


def from_spans(spans: Iterable[ReadableSpan]) -> list[Node]:
    out = []
    for span in spans:
        scope = span.instrumentation_scope
        if scope is not None and scope.name != tel.TRACER_NAME:
            continue
        context = span.context
        out.append(
            Node(
                trace_id=format(context.trace_id, "032x") if context else "",
                name=span.name,
                attributes=dict(span.attributes or {}),
                start=(span.start_time or 0) / 1e9,
                end=(span.end_time or 0) / 1e9,
            )
        )
    return out


def from_observations(observations: Iterable[Mapping[str, Any]]) -> list[Node]:
    out = []
    prefix = "attributes."
    for found in observations:
        metadata = found.get("metadata") or {}
        if metadata.get("scope.name") not in (None, tel.TRACER_NAME):
            continue
        attributes = {k[len(prefix) :]: v for k, v in metadata.items() if k.startswith(prefix)}
        out.append(
            Node(
                trace_id=str(found.get("traceId", "")),
                name=str(found.get("name", "")),
                attributes=attributes,
                start=_seconds(found.get("startTime")),
                end=_seconds(found.get("endTime") or found.get("startTime")),
            )
        )
    return out


def turns(nodes: Iterable[Node]) -> list[Turn]:
    """One `Turn` per trace that holds an `agent.turn`, oldest first."""
    by_trace: dict[str, list[Node]] = defaultdict(list)
    for node in nodes:
        by_trace[node.trace_id].append(node)
    out = [t for group in by_trace.values() if (t := _turn(group)) is not None]
    return sorted(out, key=lambda t: t.started)


def feedback(nodes: Iterable[Node]) -> list[Feedback]:
    return [
        Feedback(
            session_id=str(n.attributes.get(tel.SESSION_ID, "")),
            user_id=str(n.attributes.get(tel.USER_ID, "")),
            value=str(n.attributes.get(tel.FEEDBACK, "")),
            at=n.start,
        )
        for n in nodes
        if n.name == "agent.feedback"
    ]


def _turn(group: list[Node]) -> Turn | None:
    turn = next((n for n in group if n.name == "agent.turn"), None)
    if turn is None:
        return None
    a = turn.attributes
    run = next((n.attributes for n in group if n.name == "agent.run"), {})
    route = next((n.attributes for n in group if n.name == "agent.route"), {})
    tools = sorted((n for n in group if n.name == "agent.tool"), key=lambda n: n.start)
    chats = [n.attributes for n in sorted(group, key=lambda n: n.start) if n.name == "gen_ai.chat"]
    captured = _flag(a.get(tel.CAPTURED))
    return Turn(
        trace_id=turn.trace_id,
        run_id=str(a.get(tel.RUN_ID, "")),
        session_id=str(a.get(tel.SESSION_ID, "")),
        user_id=str(a.get(tel.USER_ID, "")),
        started=turn.start,
        duration_s=max(0.0, turn.end - turn.start),
        synthetic=_flag(a.get(tel.SYNTHETIC)),
        captured=captured,
        result=str(a.get(tel.TURN_RESULT, "")),
        rule_id=str(a.get(tel.TURN_RULE, "")),
        reply_redacted=_flag(a.get(tel.REPLY_REDACTED)),
        input=_text(a.get(tel.INPUT)) if captured else None,
        reply=_text(a.get(tel.REPLY)) if captured else None,
        route=str(route.get(tel.ROUTE_KIND, "")),
        termination=str(run.get(tel.TERMINATION, "")),
        cost_usd=float(run.get(tel.COST_USD, 0) or 0),
        model_calls=len(chats),
        malformed=int(run.get(tel.MODEL_MALFORMED, 0) or 0),
        unbacked_promise=any(n.name == "agent.promise.unbacked" for n in group),
        tools=tuple(_tool(n, captured) for n in tools),
        input_tokens=sum(int(c.get(tel.GEN_AI_INPUT_TOKENS, 0) or 0) for c in chats),
        models=tuple(
            (str(c.get(tel.GEN_AI_REQUEST_MODEL, "")), str(c.get(tel.GEN_AI_RESPONSE_MODEL, "")))
            for c in chats
        ),
    )


def _tool(node: Node, captured: bool) -> ToolUse:
    a = node.attributes
    return ToolUse(
        name=str(a.get(tel.GEN_AI_TOOL_NAME, "")),
        side_effect=str(a.get(tel.SIDE_EFFECT, "")),
        outcome=str(a.get(tel.TOOL_OUTCOME, "")),
        arguments=_json(a.get(tel.TOOL_ARGUMENTS)) if captured else None,
        result=_json(a.get(tel.TOOL_RESULT)) if captured else None,
        duration_s=max(0.0, node.end - node.start),
        truncated=_flag(a.get("agent.tool.truncated")),
    )


def _json(value: Any) -> Any:
    """Langfuse parses a JSON attribute into an object on ingest; a span holds
    the string. Either way the rule gets the object."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _seconds(stamp: Any) -> float:
    if not stamp:
        return 0.0
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


__all__ = [
    "Feedback",
    "Node",
    "ToolUse",
    "Turn",
    "feedback",
    "from_observations",
    "from_spans",
    "turns",
]
