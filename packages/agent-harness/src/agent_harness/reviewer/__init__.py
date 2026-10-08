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

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_harness import approvals as ap
from agent_harness import escalation as esc
from agent_harness import identity as ident
from agent_harness import telemetry as tel
from agent_harness.contracts import (
    Approvals,
    Clock,
    Escalation,
    EscalationOutcome,
    Escalations,
)
from agent_harness.reviewer.approvals import router as approvals_router
from agent_harness.reviewer.guard import _Desk, _desk, read_guard, review_guard


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


router = APIRouter()


@router.get("/escalations", response_model=list[Queued], summary="The queue, oldest first")
async def queue(request: Request, who: ident.Principal = Depends(read_guard)) -> list[Queued]:
    desk = _desk(request)
    moment = desk.now()
    with tel.span("agent.escalation.queue", **{tel.USER_ID: who.subject}) as span:
        rows = await desk.store.pending()
        span.set_attribute("agent.escalation.depth", len(rows))
        return [Queued.of(row, now=moment) for row in rows]


@router.get("/escalations/{escalation_id}", response_model=Queued, summary="One escalation")
async def one(
    request: Request,
    escalation_id: Annotated[str, Path(max_length=64)],
    who: ident.Principal = Depends(read_guard),
) -> Queued:
    desk = _desk(request)
    row = await desk.store.get(escalation_id)
    if row is None:
        raise HTTPException(404, "no such escalation")
    return Queued.of(row, now=desk.now())


@router.post(
    "/escalations/{escalation_id}/resolve",
    response_model=Closed,
    summary="Close it, and say what it was",
)
async def close(
    request: Request,
    escalation_id: Annotated[str, Path(max_length=64)],
    resolution: Resolution,
    who: ident.Principal = Depends(review_guard),
) -> Closed:
    """Every refusal the escalation workflow makes becomes a 409, not a 500.

    They are all *"the state does not allow that"* — already closed, lapsed
    before anyone came, or the customer trying to close their own case — and a
    reviewer who is told which one can act on it. A 500 would say the desk is
    broken when it is working exactly as designed.
    """
    desk = _desk(request)
    try:
        if desk.desk is None:
            raise HTTPException(503, "this desk cannot close escalations")
        row = await desk.desk.resolve(
            escalation_id,
            outcome=resolution.outcome,
            by=who.subject,
            by_customer=who.customer_id,
            note=resolution.note,
            now=desk.now(),
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


def build(
    store: Escalations,
    *,
    issuer: ident.Issuer,
    clock: Clock | None = None,
    desk: esc.EscalationDesk | None = None,
    approvals: Approvals | None = None,
    approver: ap.ApprovalDesk | None = None,
    pages: Sequence[APIRouter] = (),
) -> FastAPI:
    """The reviewer app, ready to mount. Wiring only — the routes are above.

    Takes the store rather than the agent: this surface never runs a turn, never
    reaches a model, and never touches the world. It reads and closes rows.
    `pages` are the agent's own pages for the desk, in its own words — routers
    mounted beside the API (the reference agent's is `/desk`).
    """
    app = FastAPI(
        title="Support desk",
        summary=(
            "Read the escalation queue and close what you have handled; "
            "read what is waiting for an approval and decide it."
        ),
        version="1",
    )
    app.state.desk = _Desk(
        store=store,
        issuer=issuer,
        clock=clock,
        desk=desk,
        approvals=approvals,
        approver=approver,
    )
    app.include_router(router)
    app.include_router(approvals_router)
    for desk_page in pages:
        app.include_router(desk_page)
    return app


__all__ = ["Closed", "Queued", "Resolution", "build"]
