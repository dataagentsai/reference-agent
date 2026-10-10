"""What the model can do.

L3 · P5. The MCP boundary — and the seam AgentTwin intercepts. Swapping the
server URL is the only difference between this agent running against the real
world and against a projection of a declared one, which is why the tool server
is a separate process rather than an in-process convenience.

P5 is the last position at which an action can be stopped while stopping it is
still cheap. Four things happen here, in this order, and none of them trusts
anything the model said:

1. the tool must be in the registry for *this identity*
2. arguments must validate against the declared input schema
3. the identity must hold the declared scope
4. a non-read effect must pass the requests store

`requests` is a sibling module: its `Requests` protocol arrives as an argument
rather than being reached for, which is the same seam working one layer down. It
is one store at two scopes since T-062 — a message name and a tool-call name
obey one rule, and which you are holding stops changing the answer.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import jsonschema
from mcp.client import Client
from opentelemetry.trace import Span

from agent_harness import requests as req
from agent_harness import telemetry as tel
from agent_harness.contracts import (
    IdempotencyKey,
    Identity,
    SideEffectClass,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UnknownTool,
)
from agent_harness.identity import Exchange
from agent_harness.tools.mcp import (
    APPROVAL_META,
    IDEMPOTENCY_META,
    META_ENTITY,
    META_REQUIRED_SCOPE,
    META_SIDE_EFFECT,
    SESSION_META,
    MCPTransport,
    ToolRejected,
    opened,
    transport_for,
)

TRUNCATION_MARK = " …[truncated from {total} characters]"

UNOFFERED = "unoffered"
"""The `tool` label on a call for a tool the surface does not offer (AACP-0023):
one value, because the requested name is whatever the model wrote."""
"""Said to the model, so a cut result reads as cut rather than as complete."""


class Transport(Protocol):
    """How tools are listed and called — nothing about whether they should be.

    The seam between *what a tool server says* and *what this agent permits*.
    MCP is one implementation; the checks in `GatedTools` hold for any.
    """

    async def advertised(self) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
        """Every tool the server offers that can be exposed, and why the rest cannot."""
        ...

    async def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        caller: Identity,
        idempotency_key: IdempotencyKey | None = None,
    ) -> ToolResult: ...


class GatedTools:
    """The four checks at P5, in order, over any transport. A `ToolClient`.

    None of them trusts anything the model said: the tool must be on this
    identity's surface, its arguments must validate, a non-read effect must pass
    the requests store, and what comes back is bounded before it can enter
    context.
    """

    def __init__(
        self, transport: Transport, *, requests: req.Requests, max_result_chars: int = 8000
    ) -> None:
        self._transport = transport
        self._requests = requests
        self._max_result_chars = max_result_chars
        self.rejected: tuple[str, ...] = ()

    async def list_tools(self, identity: Identity) -> ToolRegistry:
        """The action surface for this identity.

        MCP permits `tools/list` to vary by the authorization presented, so a
        scope-filtered surface is protocol-legal rather than a local invention.
        """
        with tel.span("agent.tools.list") as span:
            specs, rejected = await self._transport.advertised()
            visible = tuple(
                s for s in specs if not s.required_scope or identity.may(s.required_scope)
            )
            self.rejected = rejected
            span.set_attribute("agent.tools.count", len(visible))
            for offered in visible:  # AACP-0026: a tool on the surface nobody calls
                tel.counters.tools_offered.add(1, {"tool": offered.name})
            span.set_attribute("agent.tools.rejected", len(rejected))
            return ToolRegistry(tools=visible)

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
    ) -> ToolResult:
        registry = await self.list_tools(identity)
        spec = registry.get(name)
        if spec is None:
            # AACP-0023. Counted before it raises, because no tool span opens
            # for it; under one label, because the name is the model's words.
            tel.counters.tool_calls.add(1, {"tool": UNOFFERED, "outcome": "unknown"})
            raise UnknownTool(name, tuple(t.name for t in registry.tools))

        with tel.span(
            "agent.tool",
            **{
                tel.GEN_AI_TOOL_NAME: name,
                tel.SIDE_EFFECT: spec.side_effect.value,
                tel.IDEMPOTENCY_KEY: idempotency_key.value,
            },
        ) as span:
            tel.set_payload(span, tel.TOOL_ARGUMENTS, json.dumps(arguments, sort_keys=True))
            started = time.monotonic()
            outcome = "error"
            try:
                result, outcome = await self._dispatch(
                    name, arguments, identity, idempotency_key, spec, span
                )
                # JSON, so a rule reading the record can compare fields rather
                # than parse a rendering (AHC-0114's fourth group).
                shown = result.structured if result.structured is not None else result.text
                tel.set_payload(span, tel.TOOL_RESULT, json.dumps(shown, default=str))
                return result
            finally:
                # In `finally`, so a call that raised — unreachable, invalid
                # arguments — is counted as the error it was (T-055).
                span.set_attribute(tel.TOOL_OUTCOME, outcome)
                tel.counters.tool_calls.add(1, {"tool": name, "outcome": outcome})
                tel.counters.tool_duration.record(time.monotonic() - started, {"tool": name})

    async def _dispatch(
        self,
        name: str,
        arguments: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
        spec: ToolSpec,
        span: Span,
    ) -> tuple[ToolResult, str]:
        try:
            jsonschema.validate(arguments, spec.input_schema)
        except jsonschema.ValidationError:
            # Still an error on the span and in `agent.tool.calls`; apart here
            # too, because a schema the model cannot meet is its own fault
            # (AACP-0024), not the store's.
            tel.counters.tool_invalid.add(1, {"tool": name})
            raise

        if spec.side_effect is SideEffectClass.READ:
            result = await self._transport.invoke(name, arguments, caller=identity)
            return self._bound(result, span), _outcome(result)

        # One name, one outcome — T-062. The claim is taken before the call and
        # answered after it, so a repeat under this name is handed what the
        # first attempt produced rather than making a second one.
        try:
            async with req.once(
                self._requests, idempotency_key.value, scope=req.Scope.TOOL
            ) as claim:
                answer = await self._transport.invoke(
                    name, arguments, caller=identity, idempotency_key=idempotency_key
                )
                result = self._bound(answer, span)
                # An error is indefinite: the call may have landed and had its
                # reply lost, and only the far end can tell. Leaving the outcome
                # unset abandons the name, so a retry goes out under *the same*
                # one — which is the only thing that lets the far end recognise
                # it. Storing a guess here is how one refund becomes two.
                # So is a refusal the far end calls `transient` (T-095): stored,
                # it was replayed to every retry, and a timeout a second attempt
                # would have cleared became the refund's final answer.
                if claim is not None and not result.is_error and not _transient(result):
                    claim.outcome = json.loads(result.model_dump_json())
                return result, _outcome(result)
        except req.AlreadyAnswered as answered:
            span.set_attribute("agent.tool.replayed", True)
            if answered.outcome is None:
                # The call happened and what it said is gone — erased (F-056),
                # or abandoned as indefinite. Refusing is the only honest
                # answer: the effect has already landed, so repeating it is
                # wrong, and there is nothing to report. An `execution` error
                # because it is the far end's business that this is about, not
                # a fault in the call.
                span.set_attribute("agent.tool.outcome_forgotten", True)
                forgotten = ToolResult(
                    name=name,
                    text="this was already done and what it returned is no longer held",
                    is_error=True,
                    error_channel="execution",
                )
                return forgotten, "replayed"
            return ToolResult.model_validate(answered.outcome), "replayed"

    def _bound(self, result: ToolResult, span: Span) -> ToolResult:
        """AAC-0105, AOAS Q-TOOL-RESULT — a result enters context bounded.

        Measured on `for_context()`, the rendering the context boundary sends.
        The first version measured the structured payload and cut the text
        block, while the context sent the structured payload — so a large
        structured result, the common kind, went through whole (F-023).
        """
        rendered = result.for_context()
        limit = self._max_result_chars
        if len(rendered) <= limit:
            return result
        span.set_attribute("agent.tool.truncated", True)
        tel.counters.tool_truncated.add(1, {"tool": result.name})  # AACP-0004
        mark = TRUNCATION_MARK.format(total=len(rendered))
        bounded = rendered[: max(0, limit - len(mark))] + mark
        return result.model_copy(update={"text": bounded[:limit], "truncated": True})


def _transient(result: ToolResult) -> bool:
    """The far end said this may clear if asked again (`kind: transient`)."""
    said = result.structured
    return isinstance(said, dict) and said.get("kind") == "transient"


def _outcome(result: ToolResult) -> str:
    """ok · refused · error. A far system that answered `allowed: false` gave a
    correct answer, and counting it as an error would make every refusal the
    store is right to give look like an outage."""
    if result.is_error:
        return "error"
    structured = result.structured
    if isinstance(structured, dict) and structured.get("allowed") is False:
        return "refused"
    return "ok"


class MCPToolClient(GatedTools):
    """The gated tool client over MCP — what `connect()` yields."""

    def __init__(
        self,
        client: Client,
        *,
        requests: req.Requests,
        max_result_chars: int = 8000,
        exchange: Exchange | None = None,
    ) -> None:
        transport = transport_for(client, exchange=exchange)
        super().__init__(transport, requests=requests, max_result_chars=max_result_chars)


@asynccontextmanager
async def connect(
    server: Any,
    *,
    requests: req.Requests,
    max_result_chars: int = 8000,
    exchange: Exchange | None = None,
) -> AsyncIterator[MCPToolClient]:
    """Open a tool connection for the duration of one scope.

    `server` is a URL — the real tool server, or an AgentTwin projection of a
    declared world — and may be an in-process server instance in tests, so the
    adapter under test is the one that runs in production.
    """
    async with opened(server) as client:
        yield MCPToolClient(
            client, requests=requests, max_result_chars=max_result_chars, exchange=exchange
        )


__all__ = [
    "META_ENTITY",
    "META_REQUIRED_SCOPE",
    "APPROVAL_META",
    "IDEMPOTENCY_META",
    "META_SIDE_EFFECT",
    "SESSION_META",
    "GatedTools",
    "MCPToolClient",
    "MCPTransport",
    "ToolRejected",
    "Transport",
    "connect",
]
