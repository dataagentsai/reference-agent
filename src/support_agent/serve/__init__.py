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

from opentelemetry.trace import Span
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from support_agent import approvals as ap
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import requests as req
from support_agent import telemetry as tel
from support_agent.contracts import (
    Approvals,
    CheckpointStore,
    Completed,
    ConversationId,
    Escalated,
    Escalations,
    Failed,
    Identity,
    NeedsApproval,
    Refused,
)
from support_agent.contracts.failures import AgentFailure, Fault
from support_agent.entrypoint import Agent
from support_agent.serve.feedback import feedback
from support_agent.state import Conversation
from support_agent.ui import CHAT_PAGE

AgentFactory = Callable[[], AbstractAsyncContextManager[Agent]]
"""Builds an agent inside the app's own task. See `build`."""

MAX_BODY = 8 * 1024
"""A support message is a sentence. Anything larger is a mistake or an attack,
and both are cheaper to refuse than to parse."""


class BadRequest(AgentFailure):
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

    fault = Fault.REFUSED


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


def _customer(request: Request, issuer: ident.Issuer) -> Identity:
    """The customer a request's bearer token speaks for, or a refusal."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        raise BadRequest(401, "a bearer token is required")
    try:
        principal = ident.verify(token, issuer=issuer)
    except ident.InvalidSession as exc:
        # A fixed message. The library's own text is descriptive — it will
        # happily report a codec error from a malformed segment — and every word
        # of that tells whoever is probing which part of the token they got
        # wrong. The detail goes on the span, where an operator can read it and
        # an attacker cannot.
        raise BadRequest(
            401, "the session token is not valid", internal=f"{type(exc).__name__}: {exc}"
        ) from None
    try:
        return principal.as_customer()
    except ident.NotACustomer:
        # A valid session with no customer behind it, a reviewer's. 403 and not
        # 401: the token is fine, and it is not one a customer conversation may
        # run under. Never a fallback to `sub`.
        raise BadRequest(403, "this session is not a customer's") from None


async def decode(request: Request, *, issuer: ident.Issuer) -> Inbound:
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
    who = _customer(request, issuer)

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


class _NotYours(Exception):  # noqa: N818 — control flow, not a failure
    """The conversation id belongs to another customer."""


async def chat(request: Request) -> Response:
    """POST /chat — decode, find the conversation, run the turn, answer."""
    state = request.app.state
    with tel.span("http.chat") as span:
        try:
            inbound = await decode(request, issuer=state.issuer)
        except BadRequest as exc:
            span.set_attribute("http.status_code", exc.status)
            if exc.internal:
                span.set_attribute("http.refusal_detail", tel.redact(exc.internal))
            return JSONResponse({"error": exc.detail}, status_code=exc.status)

        span.set_attribute(tel.TENANT, inbound.identity.customer_id)
        # On the root span too, not only the turn's: a trace backend groups and
        # filters by the root, so a trace whose root names nobody cannot be found
        # by customer (seen in Langfuse, T-052).
        span.set_attribute(tel.USER_ID, inbound.identity.customer_id)
        try:
            conversation = await _conversation_for(state.store, inbound)
        except _NotYours:
            # Refused as *not found* rather than *forbidden*: confirming the id
            # exists tells an attacker their guess was right.
            return JSONResponse({"error": "no such conversation"}, status_code=404)
        return await _run_turn(state.agent, inbound, conversation, span)


async def _conversation_for(store: CheckpointStore, inbound: Inbound) -> Conversation | None:
    """The conversation this request continues, if it names one it may continue.

    F-006 in one function. Before the store could be read by conversation id
    this was impossible, and the handler simply could not be written — which is
    how an abstract finding became a blocker.
    """
    if inbound.conversation_id is None:
        return None
    previous = await store.latest(inbound.conversation_id)
    if previous is None:
        return None
    conversation = Conversation.decode(previous)
    if conversation.customer_id != inbound.identity.customer_id:
        raise _NotYours
    return conversation


async def _run_turn(
    agent: Agent, inbound: Inbound, conversation: Conversation | None, span: Span
) -> Response:
    """One turn, and the status code that says what it amounted to."""
    try:
        result, conversation = await agent.handle(
            inbound.text,
            identity=inbound.identity,
            conversation=conversation,
            delivery_id=inbound.delivery_id,
        )
    except req.AlreadyAnswered as again:
        # 200, not an error. The caller did the right thing by retrying; we are
        # telling them it already happened. A 4xx here would make every
        # well-behaved queue look like a client fault.
        #
        # And where the first attempt recorded what it answered, that is what
        # comes back — the same body, byte for byte. A browser whose reply was
        # lost resends and gets the reply, rather than a note saying one exists
        # somewhere it cannot reach. `status` says the run did not happen again.
        if again.outcome is not None:
            return JSONResponse({**again.outcome, "status": "already handled"}, status_code=200)
        return JSONResponse(
            {"status": "already handled", "delivery_id": inbound.delivery_id}, status_code=200
        )
    except req.StillRunning:
        return JSONResponse({"error": "this message is already being handled"}, status_code=409)

    span.set_attribute("agent.result", type(result).__name__)
    span.set_attribute(tel.SESSION_ID, conversation.conversation_id)
    reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
    return JSONResponse(
        {
            "conversation_id": conversation.conversation_id,
            "reply": reply,
            "outcome": type(result).__name__.lower(),
        },
        status_code=REPLY_STATUS.get(type(result), 200),
    )


async def health(_: Request) -> Response:
    """Liveness only — deliberately not a dependency check.

    A health endpoint that calls the model provider fails when the provider is
    slow, and an orchestrator then restarts a process that was working
    perfectly. Degradation is the agent's job to report, not the platform's to
    react to.
    """
    return JSONResponse({"status": "ok"})


async def page(_: Request) -> Response:
    return HTMLResponse(CHAT_PAGE)


def build(
    agent: Agent | AgentFactory,
    *,
    issuer: ident.Issuer,
    store: CheckpointStore | None = None,
    escalations: Escalations | None = None,
    desk: esc.EscalationDesk | None = None,
    approvals: Approvals | None = None,
    approver: ap.ApprovalDesk | None = None,
) -> Starlette:
    """Wire an agent behind HTTP. Wiring only — the handlers are module functions.

    Takes either a ready `Agent` or a **factory**: an async context manager that
    builds one. The factory form exists because the agent holds an open MCP
    connection, and a connection has to be opened and closed in the same task.
    So the factory is entered in the app's own lifespan: opened at startup,
    closed at shutdown, both inside the loop that serves requests.

    One store per concern, wired once. Given a ready agent, the desk reads the
    agent's own escalation store and `/chat` its own checkpoint store — passing a
    different one would have the reviewer working a queue the agent never writes
    to, which runs, passes every test that mocks one side, and loses every
    escalation in production. So a mismatch fails at startup.
    """
    store, escalations = _stores_of(agent, store, escalations)
    routes: list[BaseRoute] = [
        Route("/", page),
        Route("/healthz", health),
        Route("/chat", chat, methods=["POST"]),
        Route("/feedback", feedback, methods=["POST"]),
    ]
    # Mounted, not merged: `/chat` keeps the hand-written decode whose 400s and
    # opaque 401 its tests pin, while the desk gets Pydantic bodies, scoped
    # dependencies and a generated schema an ops tool can read.
    if escalations is not None:
        from support_agent import reviewer

        routes.append(
            Mount(
                "/ops",
                app=reviewer.build(
                    escalations,
                    issuer=issuer,
                    desk=desk,
                    approvals=approvals,
                    approver=approver,
                ),
            )
        )

    if isinstance(agent, Agent):
        app = Starlette(routes=routes)
        app.state.issuer, app.state.agent, app.state.store = issuer, agent, store
        return app

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with agent() as built:
            app.state.issuer, app.state.agent = issuer, built
            app.state.store = store or built.store
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def _stores_of(
    agent: Agent | AgentFactory,
    store: CheckpointStore | None,
    escalations: Escalations | None,
) -> tuple[CheckpointStore | None, Escalations | None]:
    """A ready agent's own stores; a factory's, as named by the caller."""
    if not isinstance(agent, Agent):
        return store, escalations
    return (
        _same(store, agent.store, "checkpoint"),
        _same(escalations, agent.escalations, "escalation"),
    )


def _same[T](given: T | None, the_agents: T | None, what: str) -> T | None:
    """The agent's own store, unless the caller named that same store."""
    if given is not None and the_agents is not None and given is not the_agents:
        raise ValueError(
            f"serve was handed a {what} store that is not the agent's own — the two "
            "surfaces would read and write different state"
        )
    return the_agents if the_agents is not None else given


__all__ = [
    "REPLY_STATUS",
    "AgentFactory",
    "BadRequest",
    "Inbound",
    "build",
    "chat",
    "decode",
    "health",
    "page",
]
