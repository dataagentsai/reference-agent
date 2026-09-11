"""The way in — P1, the edge.

R-017 found that nothing called `Agent.handle`. Every caller in the repository
was a test or the simulator, so the position the whole harness sits behind was
**empty**, and three separate gaps traced back to that one absence: nothing
minted a delivery id, nothing loaded a conversation, and nothing drove more than
one conversation at a time.

This is the caller. It is deliberately thin — routing, decoding, and four
decisions about where each of `handle`'s arguments comes from. Everything that
could be called judgement happens below it.

## Where each argument comes from, and why

**`text`** — the request body. The only argument the customer supplies directly,
and the only one they are allowed to.

**`identity`** — a signed token, verified here. **Never** the request body. If
`customer_id` were read from JSON the customer sends, every customer could name
themselves any other customer, and every scope check below would be enforcing a
claim the attacker wrote. The token is verified before anything else happens,
because an unverified request should not reach the store, the router, or a span.

**`conversation`** — loaded by the id the customer holds. This is the argument
that could not be supplied at all until F-006 was fixed: checkpoints were filed
under *run* id, a fresh one is minted every turn, and no caller has ever seen
one. Writing this handler is what made that unavoidable.

**`delivery_id`** — a header the caller controls (`Idempotency-Key`, matching
the convention Stripe made standard). A queue supplies its message id, a webhook
its delivery id, a browser a generated one per send. Absent, the turn runs
unguarded — stated rather than defaulted, because inventing one here would make
the duplicate check unable to ever fire.

## What this layer refuses to do

No business logic, no prompt, no tool. If a rule were enforced here it would be
enforced *only* for HTTP callers, and the queue consumer written next month
would quietly not have it. The edge decides who is asking and which conversation
this is; everything else is the agent's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.contracts import (
    CheckpointStore,
    Completed,
    ConversationId,
    Escalated,
    EscalationStore,
    Failed,
    Identity,
    NeedsApproval,
    Refused,
)
from support_agent.entrypoint import Agent
from support_agent.state import Conversation
from support_agent.ui import CHAT_PAGE

AgentFactory = Callable[[], AbstractAsyncContextManager[Agent]]
"""Builds an agent inside the app's own task. See `build`."""

MAX_BODY = 8 * 1024
"""A support message is a sentence. Anything larger is a mistake or an attack,
and both are cheaper to refuse than to parse."""


class BadRequest(Exception):
    """A refusal, split into what the caller is told and what is recorded.

    `detail` goes back over HTTP. `internal` never does. The split exists
    because the useful description of an auth failure — which segment of the
    token was malformed, what the decoder objected to — is exactly the
    description that helps somebody guessing.
    """

    def __init__(self, status: int, detail: str, *, internal: str = "") -> None:
        self.status = status
        self.detail = detail
        self.internal = internal
        super().__init__(detail)


@dataclass(frozen=True)
class Inbound:
    """One request, decoded and verified. Every field is now trustworthy.

    Separated from the handler so the decision *"is this request legitimate and
    what does it say"* is one testable function, and so the answer cannot be
    half-made — either an `Inbound` exists or the request was refused.
    """

    text: str
    identity: Identity
    conversation_id: ConversationId | None
    delivery_id: str | None


async def decode(request: Request, *, secret: str) -> Inbound:
    """Turn an HTTP request into arguments, refusing anything that is not one."""
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise BadRequest(413, f"a message may not exceed {MAX_BODY} bytes")
    try:
        body: Any = json.loads(raw or b"{}")
    except ValueError:
        raise BadRequest(400, "body is not JSON") from None
    if not isinstance(body, dict):
        raise BadRequest(400, "body must be an object")

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest(400, "text is required")

    # Identity comes from the token and only from the token. A `customer_id` in
    # the body is ignored rather than merged — silently preferring the token
    # would still leave the field there for the next reader to trust.
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        raise BadRequest(401, "a bearer token is required")
    try:
        who = ident.verify(token, secret=secret)
    except ident.InvalidSession as exc:
        # A fixed message. The library's own text is descriptive — it will
        # happily report a codec error from a malformed segment — and every word
        # of that tells whoever is probing which part of the token they got
        # wrong. The detail goes on the span, where an operator can read it and
        # an attacker cannot.
        raise BadRequest(
            401, "the session token is not valid", internal=f"{type(exc).__name__}: {exc}"
        ) from None

    cid = body.get("conversation_id")
    if cid is not None and (not isinstance(cid, str) or not cid):
        raise BadRequest(400, "conversation_id must be a non-empty string")

    return Inbound(
        text=text.strip(),
        identity=who,
        conversation_id=ConversationId(cid) if cid else None,
        delivery_id=request.headers.get("idempotency-key"),
    )


REPLY_STATUS: dict[type[object], int] = {
    Completed: 200,
    Refused: 200,
    Escalated: 202,
    NeedsApproval: 202,
    Failed: 502,
}
"""A refusal is a **200**: the agent worked correctly and the answer is no.
Returning 4xx would make every dashboard count correct behaviour as an error
rate. `NeedsApproval` is 202 — accepted, not finished. `Failed` is 502, because
the thing that failed was downstream of us.

`Escalated` moved from 200 to **202** when escalations became records. As long as
the route only produced a sentence, 200 was the honest answer: the turn really
was finished, because nothing was outstanding. Now something is — a person owes
this conversation an answer — and 202 is the same statement `NeedsApproval`
makes. The status code is the difference between *we did it* and *we owe you*."""


def build(
    agent: Agent | AgentFactory,
    *,
    secret: str,
    store: CheckpointStore | None = None,
    escalations: EscalationStore | None = None,
) -> Starlette:
    """Wire an agent behind HTTP.

    Takes either a ready `Agent` or a **factory**: an async context manager that
    builds one. The factory form exists because the agent holds an open MCP
    connection, and a connection has to be opened and closed in the same task.
    Opening it outside the app and closing it after means two tasks, which anyio
    refuses — correctly, since a cancel scope crossing tasks is how a connection
    gets left half-closed.

    So the factory is entered in the app's own lifespan: opened at startup,
    closed at shutdown, both inside the loop that serves requests. That is the
    right production shape independently of the test that forced it.
    """

    async def chat(request: Request) -> Response:
        agent = request.app.state.agent
        store = request.app.state.store
        with tel.span("http.chat") as span:
            try:
                inbound = await decode(request, secret=secret)
            except BadRequest as exc:
                span.set_attribute("http.status_code", exc.status)
                if exc.internal:
                    span.set_attribute("http.refusal_detail", tel.redact(exc.internal))
                return JSONResponse({"error": exc.detail}, status_code=exc.status)

            span.set_attribute(tel.TENANT, inbound.identity.customer_id)

            # F-006 in one line. Before the store could be read by conversation
            # id this was impossible, and the handler simply could not be
            # written — which is how an abstract finding became a blocker.
            conversation = None
            if inbound.conversation_id is not None:
                previous = await store.latest(inbound.conversation_id)
                if previous is not None:
                    conversation = Conversation.decode(previous)
                    if conversation.customer_id != inbound.identity.customer_id:
                        # Someone else's conversation id. Refused as *not found*
                        # rather than *forbidden*: confirming it exists tells an
                        # attacker their guess was right.
                        return JSONResponse({"error": "no such conversation"}, status_code=404)

            try:
                result, conversation = await agent.handle(
                    inbound.text,
                    identity=inbound.identity,
                    conversation=conversation,
                    delivery_id=inbound.delivery_id,
                )
            except trg.DuplicateDelivery:
                # 200, not an error. The caller did the right thing by retrying;
                # we are telling them it already happened. A 4xx here would make
                # every well-behaved queue look like a client fault.
                return JSONResponse(
                    {"status": "already handled", "delivery_id": inbound.delivery_id},
                    status_code=200,
                )
            except trg.OverlappingRun:
                return JSONResponse(
                    {"error": "this message is already being handled"}, status_code=409
                )

            span.set_attribute("agent.result", type(result).__name__)
            return JSONResponse(
                {
                    "conversation_id": conversation.conversation_id,
                    "reply": getattr(result, "reply", "")
                    or getattr(result, "customer_message", ""),
                    "outcome": type(result).__name__.lower(),
                },
                status_code=REPLY_STATUS.get(type(result), 200),
            )

    async def health(_: Request) -> Response:
        """Liveness only — deliberately not a dependency check.

        A health endpoint that calls the model provider fails when the provider
        is slow, and an orchestrator then restarts a process that was working
        perfectly. Degradation is the agent's job to report, not the platform's
        to react to.
        """
        return JSONResponse({"status": "ok"})

    async def page(_: Request) -> Response:
        return HTMLResponse(CHAT_PAGE)

    routes: list[BaseRoute] = [
        Route("/", page),
        Route("/healthz", health),
        Route("/chat", chat, methods=["POST"]),
    ]

    # Mounted, not merged. FastAPI *is* Starlette, so this is the whole
    # integration — and `/chat` keeps the hand-written decode whose 400s and
    # opaque 401 its tests pin, while the desk gets Pydantic bodies, scoped
    # dependencies and a generated schema an ops tool can read. Neither surface
    # pays for the other's decisions.
    if escalations is not None:
        from support_agent import reviewer

        routes.append(Mount("/ops", app=reviewer.build(escalations, secret=secret)))

    if isinstance(agent, Agent):
        app = Starlette(routes=routes)
        app.state.agent = agent
        app.state.store = store or agent.store
        return app

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with agent() as built:
            app.state.agent = built
            app.state.store = store or built.store
            yield

    return Starlette(routes=routes, lifespan=lifespan)


__all__ = ["REPLY_STATUS", "AgentFactory", "BadRequest", "Inbound", "build", "decode"]
