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
4. a non-read effect must pass the idempotency ledger

`idempotency` is a sibling module and may not be imported. The ledger arrives as
an `IdempotencyLedger` — the protocol from `contracts` — which is the same seam
working one layer down.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

import jsonschema
from mcp.client import Client
from mcp_types import RequestParamsMeta
from opentelemetry.trace import Span

from support_agent import telemetry as tel
from support_agent.contracts import (
    IdempotencyKey,
    IdempotencyLedger,
    Identity,
    SideEffectClass,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
    UnknownTool,
)

SESSION_META = "aoas/session"
"""Where the caller's verified session travels in a call's `_meta`, so the
system that owns a row can decide whose it is (F-016). The key is a binding the
tool server and this transport share — declared on both sides, because the agent
may not import its simulator — and belongs in the binding spec."""

META_SIDE_EFFECT = "side_effect"
META_REQUIRED_SCOPE = "required_scope"


class ToolRejected(Exception):
    """A tool the server advertised that we refuse to expose to the model."""


def _spec_from(tool: Any) -> ToolSpec:
    """Translate an advertised tool, or refuse it.

    Two rejections, both fail-closed:

    **No `output_schema`.** Optional in the MCP specification, mandatory here.
    Where a tool declares one, servers MUST return structured results conforming
    to it — which is what turns a world-state assertion from parsing into
    comparison. A tool without one cannot be asserted against cheaply.

    **No declared side effect.** Defaulting to READ would let an undeclared
    refund tool skip the idempotency ledger entirely, so silence is refused
    rather than assumed. Nothing is exposed without naming what it costs to
    repeat.
    """
    meta = dict(getattr(tool, "meta", None) or {})
    if not getattr(tool, "output_schema", None):
        raise ToolRejected(f"{tool.name}: no outputSchema")
    raw_effect = meta.get(META_SIDE_EFFECT)
    if raw_effect is None:
        raise ToolRejected(f"{tool.name}: no declared side effect")
    try:
        side_effect = SideEffectClass(raw_effect)
    except ValueError as exc:
        raise ToolRejected(f"{tool.name}: unknown side effect {raw_effect!r}") from exc

    return ToolSpec(
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.input_schema or {}),
        output_schema=dict(tool.output_schema),
        side_effect=side_effect,
        required_scope=meta.get(META_REQUIRED_SCOPE),
    )


class Transport(Protocol):
    """How tools are listed and called — nothing about whether they should be.

    The seam between *what a tool server says* and *what this agent permits*.
    MCP is one implementation; the checks in `GatedTools` hold for any.
    """

    async def advertised(self) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
        """Every tool the server offers that can be exposed, and why the rest cannot."""
        ...

    async def invoke(
        self, name: str, arguments: dict[str, object], *, caller: Identity
    ) -> ToolResult: ...


class MCPTransport:
    """Speaks MCP. The only class that may.

    Deliberately owns no connection lifecycle: an earlier version drove
    `__aenter__`/`__aexit__` by hand, which breaks under structured concurrency
    the moment entry and exit land in different tasks. `connect()` below owns
    the `async with`; this is a pure adapter over a live client.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    async def advertised(self) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
        try:
            listed = await self._client.list_tools()
        except Exception as exc:
            raise ToolUnavailable(str(exc)) from exc
        specs: list[ToolSpec] = []
        rejected: list[str] = []
        for tool in listed.tools:
            try:
                specs.append(_spec_from(tool))
            except ToolRejected as exc:
                rejected.append(str(exc))
        return tuple(specs), tuple(rejected)

    async def invoke(
        self, name: str, arguments: dict[str, object], *, caller: Identity
    ) -> ToolResult:
        """Both MCP failure channels, kept apart — and the caller carried with the call.

        The session travels as request metadata, not as an argument: the model
        writes the arguments, and whose session this is must never be something
        the model can say.

        A protocol error (unknown tool, malformed request) raises and surfaces
        here; an execution error comes back as an ordinary result with
        `is_error` set. An agent that handles one and not the other looks
        healthy until production.
        """
        try:
            # MCP's `_meta` is an open object; the SDK's TypedDict names only the
            # progress token. The cast states that gap rather than hiding it.
            meta = cast(RequestParamsMeta, {SESSION_META: {"customer_id": caller.customer_id}})
            raw = await self._client.call_tool(name, arguments, meta=meta)
        except Exception as exc:
            return ToolResult(name=name, text=str(exc), is_error=True, error_channel="protocol")

        structured = getattr(raw, "structured_content", None)
        text = "".join(
            getattr(block, "text", "") for block in (getattr(raw, "content", None) or [])
        )
        is_error = bool(getattr(raw, "is_error", False))
        return ToolResult(
            name=name,
            structured=structured,
            text=text,
            is_error=is_error,
            error_channel="execution" if is_error else "none",
        )


class GatedTools:
    """The four checks at P5, in order, over any transport. A `ToolClient`.

    None of them trusts anything the model said: the tool must be on this
    identity's surface, its arguments must validate, a non-read effect must pass
    the idempotency ledger, and what comes back is bounded before it can enter
    context.
    """

    def __init__(
        self, transport: Transport, *, ledger: IdempotencyLedger, max_result_chars: int = 8000
    ) -> None:
        self._transport = transport
        self._ledger = ledger
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
            raise UnknownTool(name, tuple(t.name for t in registry.tools))

        with tel.span(
            "agent.tool",
            **{
                tel.GEN_AI_TOOL_NAME: name,
                tel.SIDE_EFFECT: spec.side_effect.value,
                tel.IDEMPOTENCY_KEY: idempotency_key.value,
            },
        ) as span:
            jsonschema.validate(arguments, spec.input_schema)

            if spec.side_effect is SideEffectClass.READ:
                result = await self._transport.invoke(name, arguments, caller=identity)
                return self._bound(result, span)

            previous = await self._ledger.seen(idempotency_key)
            if previous is not None:
                span.set_attribute("agent.tool.replayed", True)
                return previous

            result = self._bound(
                await self._transport.invoke(name, arguments, caller=identity), span
            )
            if not result.is_error:
                await self._ledger.record(idempotency_key, result)
            return result

    def _bound(self, result: ToolResult, span: Span) -> ToolResult:
        """AAC-0105 — tool results are bounded before they enter context.

        The structured payload is what assertions read, so it is measured by
        serialised size rather than by the prose length of the text block.
        """
        serialised = json.dumps(result.structured, default=str) if result.structured else ""
        limit = self._max_result_chars
        if len(serialised) <= limit and len(result.text) <= limit:
            return result
        span.set_attribute("agent.tool.truncated", True)
        return result.model_copy(update={"text": result.text[:limit], "truncated": True})


class MCPToolClient(GatedTools):
    """The gated tool client over MCP — what `connect()` yields."""

    def __init__(
        self, client: Client, *, ledger: IdempotencyLedger, max_result_chars: int = 8000
    ) -> None:
        super().__init__(MCPTransport(client), ledger=ledger, max_result_chars=max_result_chars)


@asynccontextmanager
async def connect(
    server: Any,
    *,
    ledger: IdempotencyLedger,
    max_result_chars: int = 8000,
) -> AsyncIterator[MCPToolClient]:
    """Open a tool connection for the duration of one scope.

    `server` is a URL — the real tool server, or an AgentTwin projection of a
    declared world — and may be an in-process server instance in tests, so the
    adapter under test is the one that runs in production.
    """
    async with Client(server) as client:
        yield MCPToolClient(client, ledger=ledger, max_result_chars=max_result_chars)


__all__ = [
    "META_REQUIRED_SCOPE",
    "META_SIDE_EFFECT",
    "SESSION_META",
    "GatedTools",
    "MCPToolClient",
    "MCPTransport",
    "ToolRejected",
    "Transport",
    "connect",
]
