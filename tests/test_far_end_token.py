"""The agent's side of A1: a token for the far end on every MCP call.

1. The exchange, one contract table on every identity adapter that has one
   (`local-dev`, `entra-id`): a session in, a token addressed to the far end
   out, carrying the holder; no session, no token; the approval worker's own
   login carrying no holder. Built through the registry; Entra's token
   endpoint answered in process.
2. Where the token travels: over streamable HTTP, the `Authorization` header of
   that call's own request, and gone from its `_meta`; in process, `_meta`.
   The MCP client has no per-call header (`tools.mcp.BearerFromSession`).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

import httpx2 as httpx
import jwt
import pytest
import uvicorn
from mcp.server.mcpserver import Context, MCPServer

from agent_harness import identity as ident
from agent_harness.adapters import ADAPTERS, Wiring
from agent_harness.contracts import IdempotencyKey, Identity, RunId
from agent_harness.identity.local import LocalIssuer
from agent_harness.requests import InMemoryRequests
from agent_harness.tools import connect
from agent_harness.tools.mcp import SESSION_META, lifted

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT_URL = "http://local-issuer.test/realms/claims"
FAR = "claims-system"
TURN_SCOPES = frozenset({"claims:read", "claims:write"})
WORKER_SCOPES = ["claims:read", "payouts:write"]
FAKE_ENTRA = LocalIssuer(url=f"https://login.microsoftonline.com/{TENANT}/v2.0", audience=FAR)
"""What the fake token endpoint signs with: the far end's tokens, as Entra would."""


def entra_endpoint() -> httpx.MockTransport:
    """The tenant's token endpoint: on-behalf-of mints for the assertion's
    holder; client credentials for the app, with its role and no holder."""

    def answer(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form["grant_type"] == "client_credentials":
            token = FAKE_ENTRA.mint(subject="agent-app", scopes=set(), party="agent-app")
        else:
            said = jwt.decode(form["assertion"], options={"verify_signature": False})
            token = FAKE_ENTRA.mint(
                subject=said["sub"], scopes=TURN_SCOPES, customer_id=said.get("customer_id")
            )
        return httpx.Response(200, json={"access_token": token, "expires_in": 3600})

    return httpx.MockTransport(answer)


EXCHANGING: dict[str, dict[str, Any]] = {
    "local-dev": {
        "url": AGENT_URL,
        "audience": "claims-fnol",
        "far_end_audience": FAR,
        "worker_scopes": WORKER_SCOPES,
    },
    "entra-id": {
        "tenant": TENANT,
        "audience": "claims-fnol",
        "client_id": "agent-app",
        "client_secret": "s",
        "far_end_scope": f"api://{FAR}/.default",
        "worker_scope": f"api://{FAR}/.default",
    },
}
SESSION_SIGNER = LocalIssuer(url=f"https://login.microsoftonline.com/{TENANT}/v2.0")


@asynccontextmanager
async def sessions(name: str, settings: dict[str, Any]) -> AsyncIterator[Any]:
    chosen = ADAPTERS.load("identity", name)
    filled = {k: settings.get(k, s.default) for k, s in chosen.settings.items()}
    hooks = {"keys": ident.JWKS(SESSION_SIGNER.jwks), "transport": entra_endpoint()}
    async with chosen.build(Wiring(filled, {}, hooks, "test")) as product:
        yield product


def session_of(product: Any, holder: str = "PH-1001") -> Identity:
    """A policyholder's session as the chat edge hands it on."""
    signer = product.signer or SESSION_SIGNER
    token = signer.mint(
        subject="login-1", scopes=TURN_SCOPES, customer_id=holder, audience="claims-fnol"
    )
    return Identity(customer_id=holder, scopes=TURN_SCOPES, token=token)


def claims_of(token: str) -> dict[str, Any]:
    said: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return said


# (row, which login, whether the identity has a session, holder or "refused")
ROWS: list[tuple[str, str, bool, str | None]] = [
    ("a session is exchanged for a token addressed to the far end", "exchange", True, "PH-1001"),
    ("no session, no token", "exchange", False, "refused"),
    ("the worker's own login names no holder", "worker", False, None),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize("name", sorted(EXCHANGING))
@pytest.mark.parametrize(("row", "login", "has_session", "holder"), ROWS, ids=[r[0] for r in ROWS])
async def test_the_exchange_port(
    name: str, row: str, login: str, has_session: bool, holder: str | None
) -> None:
    async with sessions(name, EXCHANGING[name]) as product:
        identity = session_of(product)
        if not has_session:
            identity = identity.model_copy(update={"token": None})
        exchange = getattr(product, login)
        if holder == "refused":
            with pytest.raises(ident.InvalidSession):
                await exchange.for_far_end(identity)
            return
        claims = claims_of(await exchange.for_far_end(identity))
    assert claims["aud"] == FAR, row
    assert claims.get(ident.CLAIM_CUSTOMER) == holder, row


@pytest.mark.discharges("AAC-0057")
@pytest.mark.parametrize("name", sorted(EXCHANGING))
async def test_without_far_end_settings_nothing_is_exchanged(name: str) -> None:
    bare = {k: v for k, v in EXCHANGING[name].items() if k in ("url", "audience", "tenant")}
    async with sessions(name, bare) as product:
        assert (product.exchange, product.worker) == (None, None)


@pytest.mark.discharges("AAC-0057")
async def test_the_local_exchange_carries_only_what_session_and_turn_both_hold() -> None:
    async with sessions("local-dev", EXCHANGING["local-dev"]) as product:
        identity = session_of(product)
        widened = identity.model_copy(update={"scopes": identity.scopes | {"payouts:write"}})
        token = await product.exchange.for_far_end(widened)
        far = product.signer.issuer()
        far = ident.Issuer(far.url, FAR, far.keys)
        verified = ident.verify(token, issuer=far)
    assert verified.scopes == TURN_SCOPES, "a scope added in process does not reach the far end"
    assert claims_of(token)["exp"] - int(time.time()) <= 300, "short-lived"


# ------------------------------------------------------------ where it travels
def request(body: Any, method: str = "POST") -> httpx.Request:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Request(method, "http://far.test/mcp", content=content)


def call(session: dict[str, Any]) -> dict[str, Any]:
    params = {"name": "t", "arguments": {}, "_meta": {SESSION_META: session}}
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


# (row, the request, the header it leaves with, the session left in its body)
LIFTED: list[tuple[str, httpx.Request, str | None, dict[str, Any] | None]] = [
    (
        "a call's token becomes its bearer header and leaves the body",
        request(call({"customer_id": "PH-1001", "token": "tok"})),
        "Bearer tok",
        {"customer_id": "PH-1001"},
    ),
    (
        "a call with no token is sent as it was",
        request(call({"customer_id": "PH-1001"})),
        None,
        {"customer_id": "PH-1001"},
    ),
    ("a GET is sent as it was", request(b"", "GET"), None, None),
    ("a body that is not JSON is sent as it was", request(b"<x/>"), None, None),
    ("a batch is sent as it was", request([call({"token": "tok"})]), None, None),
]


@pytest.mark.discharges("AAC-0057")
@pytest.mark.parametrize(("row", "sent", "header", "left"), LIFTED, ids=[r[0] for r in LIFTED])
def test_the_token_is_lifted_into_its_own_requests_header(
    row: str, sent: httpx.Request, header: str | None, left: dict[str, Any] | None
) -> None:
    out = lifted(sent)
    assert out.headers.get("authorization") == header, row
    if left is not None:
        assert json.loads(out.content)["params"]["_meta"][SESSION_META] == left
        assert int(out.headers["content-length"]) == len(out.content)


def echo_server() -> MCPServer:
    """A far end that says what reached it: the header, and the session in `_meta`."""
    server = MCPServer("echo")

    @server.tool(meta={"side_effect": "read"}, structured_output=True)
    async def seen(ctx: Context | None = None) -> dict[str, Any]:
        assert ctx is not None
        http = ctx.request_context.request
        meta = ctx.request_context.meta
        extra = meta if isinstance(meta, dict) else dict(getattr(meta, "model_extra", {}) or {})
        return {
            "header": http.headers.get("authorization") if http is not None else None,
            "session": extra.get(SESSION_META),
        }

    return server


@asynccontextmanager
async def served(server: MCPServer) -> AsyncIterator[str]:
    """The server over streamable HTTP on a free port, for the scope."""
    config = uvicorn.Config(server.streamable_http_app(), port=0, log_level="warning")
    running = uvicorn.Server(config)
    task = asyncio.create_task(running.serve())
    while not running.started:
        await asyncio.sleep(0.02)
    port = running.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.should_exit = True
        await task


class Fixed:
    async def for_far_end(self, identity: Identity) -> str:
        return f"far-{identity.customer_id}-{uuid.uuid4().hex[:4]}"


# (row, over HTTP or in process, the header the far end sees, token left in `_meta`)
TRAVEL = [
    ("over HTTP the token is the call's bearer header", True, True, False),
    ("in process the token stays in _meta", False, False, True),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "http", "header", "in_meta"), TRAVEL, ids=[t[0] for t in TRAVEL])
async def test_where_the_far_ends_token_travels(
    row: str, http: bool, header: bool, in_meta: bool
) -> None:
    caller = Identity(customer_id="PH-1001", scopes=TURN_SCOPES, token="session")
    key = IdempotencyKey(run_id=RunId("run_far"), step=1, iteration=1)
    server = echo_server()

    async def ask(target: Any) -> dict[str, Any]:
        async with connect(target, requests=InMemoryRequests(), exchange=Fixed()) as tools:
            said: dict[str, Any] = (await tools.call("seen", {}, caller, key)).structured
            return said

    if http:
        async with served(server) as url:
            said = await ask(url)
    else:
        said = await ask(server)
    assert str(said["header"]).startswith("Bearer far-PH-1001-") is header, row
    assert ("token" in (said["session"] or {})) is in_meta, row
    assert said["session"]["customer_id"] == "PH-1001"
