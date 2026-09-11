"""The actor for the queue — P8, and the half F-007 keeps finding missing.

`agent_state.escalations` has held rows since this morning and nothing could
read them. A queue with no surface is a queue whose entries are, from any
operator's point of view, indistinguishable from lost.

## Why this one is FastAPI when `/chat` is not

`serve` is hand-wired Starlette on purpose: one route, an error contract pinned
by tests — 400 where FastAPI's validation answers 422, and a 401 body kept
deliberately opaque because the library's own message tells whoever is probing
which part of the token they got wrong. Converting it would risk that contract
to buy documentation nobody reads.

This surface is the opposite shape. Several routes, **two scopes**, request
bodies with real structure, and an audience that is not a browser we also wrote
— an ops tool integrates against it. `Depends(requires(...))` written once beats
a scope check repeated in every handler and forgotten in one, and the generated
schema is the contract that audience reads.

So both live in one process, mounted rather than merged, and neither pays for
the other's decisions. FastAPI *is* Starlette; a mount is the whole integration.

## What the desk may not do

It may not act as the customer. `REVIEWER_SCOPES` holds no `orders:*` at all, so
nothing here can cancel, refund or amend anything — a reviewer who needs to do
that does it as themselves through the ordinary surface, under the same
authorisation as anyone else.

And a customer can never reach these routes, which is what turns `resolve`'s
refusal into a control: the outcome label is the input to the over-escalation
rate, and the party being measured must not be able to write it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import (
    Clock,
    Escalation,
    EscalationOutcome,
    EscalationStore,
    Identity,
)


class Queued(BaseModel):
    """One row, as the desk sees it.

    A projection rather than the record itself. `conversation_id` and `run_id`
    are here because a reviewer picking this up needs to find the conversation;
    `rules_version` is not, because nothing a person does with this row depends
    on it — it is for the analysis, and the analysis reads the table.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    conversation_id: str
    customer_id: str
    reason: str
    rule_id: str
    tier: int
    created_at: int
    expires_at: int
    waiting_s: int
    """How long it has been queued. Computed rather than stored, because a
    reviewer reads *"nineteen minutes"* and never an epoch."""

    @classmethod
    def of(cls, row: Escalation, *, now: int) -> Queued:
        return cls(
            id=row.id,
            conversation_id=row.conversation_id,
            customer_id=row.customer_id,
            reason=row.reason,
            rule_id=row.rule_id,
            tier=row.tier,
            created_at=row.created_at,
            expires_at=row.expires_at,
            waiting_s=max(0, now - row.created_at),
        )


class Resolution(BaseModel):
    """What a reviewer says on the way out.

    `outcome` has no default. A close that does not say whether the agent could
    have handled it teaches us nothing, and a default would let the most
    common — and least informative — answer be the one nobody had to choose.
    """

    model_config = ConfigDict(frozen=True)

    outcome: EscalationOutcome
    note: str = Field(default="", max_length=2000)


class Closed(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    state: str
    outcome: str
    outcome_by: str
    waited_s: int


def build(store: EscalationStore, *, secret: str, clock: Clock | None = None) -> FastAPI:
    """The reviewer app, ready to mount.

    Takes the store rather than the agent: this surface never runs a turn, never
    reaches a model, and never touches the world. It reads and closes rows.
    """

    app = FastAPI(
        title="Escalation desk",
        summary="Read the escalation queue and close what you have handled.",
        version="1",
    )

    def now() -> int:
        import time

        return clock() if clock is not None else int(time.time())

    def reviewer(authorization: str | None = Header(default=None)) -> Identity:
        """Verify once, here, and hand every route a trustworthy identity.

        The 401 body is fixed for the same reason `serve`'s is: the library's own
        message is descriptive enough to tell a prober which part of the token
        was wrong. The detail goes on the span, where an operator can read it and
        an attacker cannot.
        """
        header = authorization or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token:
            raise HTTPException(401, "a bearer token is required")
        try:
            return ident.verify(token, secret=secret)
        except ident.InvalidSession as exc:
            with tel.span(
                "agent.escalation.refused",
                **{"http.refusal_detail": tel.redact(f"{type(exc).__name__}: {exc}")},
            ):
                pass
            raise HTTPException(401, "the session token is not valid") from None

    def requires(scope: str) -> Callable[..., Identity]:
        """One scope check, declared per route.

        The reason this surface is worth a framework: written once, applied by
        the router, and impossible to forget on the route added next month.
        """

        def guard(who: Identity = Depends(reviewer)) -> Identity:
            if not who.may(scope):
                raise HTTPException(403, f"this session does not hold {scope}")
            return who

        return guard

    # `Depends(...)` as the default rather than inside `Annotated`: `Identity` is
    # a Pydantic model, and FastAPI reads a bare model-typed parameter as a
    # request field — so the annotated form silently became a *query parameter
    # named `who`*, and every route answered 422 before the token was ever read.
    read_guard = requires(ident.SCOPE_ESCALATIONS_READ)
    review_guard = requires(ident.SCOPE_ESCALATIONS_REVIEW)

    @app.get("/escalations", response_model=list[Queued], summary="The queue, oldest first")
    async def queue(who: Identity = Depends(read_guard)) -> list[Queued]:
        moment = now()
        with tel.span("agent.escalation.queue", **{tel.TENANT: who.customer_id}) as span:
            rows = await store.pending()
            span.set_attribute("agent.escalation.depth", len(rows))
            return [Queued.of(row, now=moment) for row in rows]

    @app.get("/escalations/{escalation_id}", response_model=Queued, summary="One escalation")
    async def one(
        escalation_id: Annotated[str, Path(max_length=64)],
        who: Identity = Depends(read_guard),
    ) -> Queued:
        row = await store.get(escalation_id)
        if row is None:
            raise HTTPException(404, "no such escalation")
        return Queued.of(row, now=now())

    @app.post(
        "/escalations/{escalation_id}/resolve",
        response_model=Closed,
        summary="Close it, and say what it was",
    )
    async def close(
        escalation_id: Annotated[str, Path(max_length=64)],
        resolution: Resolution,
        who: Identity = Depends(review_guard),
    ) -> Closed:
        """Every refusal `esc.resolve` raises becomes a 409, not a 500.

        They are all *"the state does not allow that"* — already closed, lapsed
        before anyone came, or the customer trying to close their own case — and
        a reviewer who is told which one can act on it. A 500 would tell them the
        desk is broken when the desk is working exactly as designed.
        """
        try:
            row = await esc.resolve(
                store,
                escalation_id,
                outcome=resolution.outcome,
                by=who.customer_id,
                note=resolution.note,
                now=now(),
            )
        except esc.EscalationError as refused:
            detail = str(refused)
            raise HTTPException(404 if "no escalation" in detail else 409, detail) from None

        return Closed(
            id=row.id,
            state=row.state.value,
            outcome=row.outcome.value if row.outcome else "",
            outcome_by=row.outcome_by or "",
            waited_s=max(0, (row.resolved_at or 0) - row.created_at),
        )

    return app


__all__ = ["Closed", "Queued", "Resolution", "build"]
