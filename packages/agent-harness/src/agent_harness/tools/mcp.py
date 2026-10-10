"""The MCP adapter — the only module that speaks the protocol.

Split out of `tools/__init__.py` when that file hit its size ceiling, and the
split is the one the package docstring already described: *what a tool server
says* is this file, and *what this agent permits* is `GatedTools` next door. The
import contract names `support_agent.tools.*` as the only place the `mcp`
package may be imported, so the boundary moves with the code.

`_spec_from` comes with it. It reads an MCP tool's `meta` — the side-effect
class and the required scope — and nothing outside this protocol has a `meta`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Generator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import RequestParamsMeta

from agent_harness.contracts import (
    IdempotencyKey,
    Identity,
    SideEffectClass,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
)
from agent_harness.contracts.failures import AgentFailure, Fault
from agent_harness.identity import Exchange
from agent_harness.telemetry.redaction import redact

SESSION_META = "aoas/session"
"""Where the caller's verified session travels in a call's `_meta`, so the
system that owns a row can decide whose it is (F-016). The key is a binding the
tool server and this transport share — declared on both sides, because the agent
may not import its simulator — and belongs in the binding spec."""

IDEMPOTENCY_META = "aoas/idempotency-key"
"""Where a write's idempotency key travels, so the system the effect lands on can
recognise a retry — the harness's store cannot, when the effect landed and the
reply was lost (F-017). The same binding as `SESSION_META`."""

APPROVAL_META = "aoas/approval"
"""Where an elevated call names the approval that elevated it. The far end loads
that approval and checks it matches the call, rather than trusting a scope the
agent added to its own identity (T-002). The same binding as `SESSION_META`."""


class ToolRejected(AgentFailure):
    """A tool the server advertised that we refuse to expose to the model."""

    fault = Fault.MISCONFIGURED


META_SIDE_EFFECT = "side_effect"
META_ENTITY = "entity"
META_REQUIRED_SCOPE = "required_scope"


def _spec_from(tool: Any) -> ToolSpec:
    """Translate an advertised tool, or refuse it.

    Two rejections, both fail-closed:

    **No `output_schema`.** Optional in the MCP specification, mandatory here.
    Where a tool declares one, servers MUST return structured results conforming
    to it — which is what turns a world-state assertion from parsing into
    comparison. A tool without one cannot be asserted against cheaply.

    **No declared side effect.** Defaulting to READ would let an undeclared
    refund tool skip the requests store entirely, so silence is refused
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
        # Absent is allowed and means "this server does not say". Only the
        # re-read in `freshness` reads it, and it falls back when nothing
        # declares one — so a server that has never heard of it is unaffected.
        entity=str(meta.get(META_ENTITY) or ""),
    )


class MCPTransport:
    """Speaks MCP. The only class that may.

    Deliberately owns no connection lifecycle: an earlier version drove
    `__aenter__`/`__aexit__` by hand, which breaks under structured concurrency
    the moment entry and exit land in different tasks. `connect()` below owns
    the `async with`; this is a pure adapter over a live client.
    """

    def __init__(self, client: Client, *, exchange: Exchange | None = None) -> None:
        self._client = client
        self._exchange = exchange

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
        self,
        name: str,
        arguments: dict[str, object],
        *,
        caller: Identity,
        idempotency_key: IdempotencyKey | None = None,
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
            fields: dict[str, object] = {SESSION_META: await self._session(caller)}
            if caller.grant is not None:
                fields[APPROVAL_META] = caller.grant
            if idempotency_key is not None:
                fields[IDEMPOTENCY_META] = idempotency_key.value
            meta = cast(RequestParamsMeta, fields)
            raw = await self._client.call_tool(name, arguments, meta=meta)
        except Exception as exc:
            # Into the model's context, so scrubbed first: a far end's error can
            # quote the token or key it refused (AHC-0035, F-074). The kind of
            # failure stays, because the model chooses its next step by it.
            said = f"{type(exc).__name__}: {redact(str(exc))}"
            return ToolResult(name=name, text=said, is_error=True, error_channel="protocol")

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

    async def _session(self, caller: Identity) -> dict[str, object]:
        """What the far end is told about the caller.

        With an exchange, a token addressed to the far end, which it verifies;
        the asserted `customer_id` rides along for a stand-in that has no
        verifier, and a far end that has one ignores it (T-002). Without an
        exchange, the assertion alone, which only a simulation should accept.
        """
        session: dict[str, object] = {"customer_id": caller.customer_id}
        # Over HTTP the token leaves `_meta` for the Authorization header on
        # its way out (`BearerFromSession`); in process it stays here.
        if self._exchange is not None:
            session["token"] = await self._exchange.for_far_end(caller)
        return session


class BearerFromSession(httpx2.Auth):
    """The far end's token, moved from the call's `_meta` to its `Authorization`
    header, on the streamable HTTP transport (A1).

    The MCP client has no per-call header: `call_tool` takes `meta` and nothing
    that reaches the HTTP request, and one connection carries every turn's
    calls, so a header set on the connection would be one turn's token on
    another's call. So the token travels with its own call, in `_meta`, as far
    as the HTTP client, and this auth flow lifts it out of that request's body
    into that request's header — one request, one token, no shared state.
    In process (no HTTP) nothing runs here and the token stays in `_meta`."""

    requires_request_body = True

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        yield lifted(request)


def lifted(request: httpx2.Request) -> httpx2.Request:
    """`request` with its session's token as a bearer header, or as it was."""
    try:
        body = json.loads(request.content) if request.method == "POST" else None
    except ValueError:
        return request
    params = body.get("params") if isinstance(body, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    session = meta.get(SESSION_META) if isinstance(meta, dict) else None
    token = session.pop("token", None) if isinstance(session, dict) else None
    if not isinstance(token, str) or not token:
        return request
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    headers["authorization"] = f"Bearer {token}"
    return httpx2.Request(
        request.method,
        request.url,
        headers=headers,
        content=json.dumps(body).encode(),
        extensions=request.extensions,
    )


@asynccontextmanager
async def opened(server: Any) -> AsyncIterator[Client]:
    """An MCP client for `server`: a URL over streamable HTTP with the bearer
    lifted into the header, or anything else `Client` takes (an in-process
    server, in tests) as it is."""
    async with AsyncExitStack() as stack:
        target = server
        if isinstance(server, str):
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    auth=BearerFromSession(),
                    follow_redirects=True,
                    timeout=httpx2.Timeout(30.0, read=300.0),
                )
            )
            target = streamable_http_client(server, http_client=http)
        yield await stack.enter_async_context(Client(target))


def transport_for(client: Client, *, exchange: Exchange | None = None) -> MCPTransport:
    """Build the adapter — here, because this is where it is defined.

    The composition rule is that a realisation is constructed only in its own
    module, and `MCPToolClient` next door needs one. A one-line factory is what
    keeps that rule true across the split rather than an exception to it.
    """
    return MCPTransport(client, exchange=exchange)
