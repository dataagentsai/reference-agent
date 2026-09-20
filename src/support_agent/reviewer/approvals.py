"""The approvals desk: read what is waiting, and decide it (T-059).

Beside the escalation routes and deliberately not the same authority. Closing an
escalation costs a colleague's time; granting an approval moves money, so it is
its own scope and can be held by a different person — finance rather than
support — without anything else changing.

**The desk decides; it does not act.** A grant goes to the workflow, whose
validator holds the rules — not your own request, not one already decided, not
one that lapsed while you were reading it — and the workflow then carries the
refund out under its own login (T-028). Nobody at a desk holds `refunds:write`.

Every refusal the workflow makes becomes a **409**, as on the escalation side: a
state that does not allow the decision is a fact a person can act on, and a 500
would say the desk is broken when it is working exactly as designed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Approval
from support_agent.reviewer.guard import (
    _Desk,
    _desk,
    approvals_decide_guard,
    approvals_read_guard,
)


class Waiting(BaseModel):
    """One approval, as the desk sees it.

    `args` is included where the escalation projection leaves its innards out:
    an approver cannot decide a refund without seeing which order it is for.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, object]
    reason: str
    customer_id: str
    state: str
    created_at: int
    expires_at: int
    waiting_s: int
    """How long it has been waiting. Computed, because a person reads
    *"nineteen minutes"* and never an epoch."""

    @classmethod
    def of(cls, row: Approval, *, now: int) -> Waiting:
        return cls(
            id=row.id,
            action=row.action,
            args=dict(row.args),
            reason=row.reason,
            customer_id=row.customer_id,
            state=row.state.value,
            created_at=row.created_at,
            expires_at=row.expires_at,
            waiting_s=max(0, now - row.created_at),
        )


class Decision(BaseModel):
    """What the person decided. `granted` is required and has no default: a
    body that forgot to say is a body nobody should guess at."""

    model_config = ConfigDict(frozen=True)

    granted: bool
    note: str = Field(default="", max_length=2000)


class Decided(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    state: str
    granted: bool
    decided_by: str
    result: str
    waited_s: int


router = APIRouter()


def _readable(request: Request) -> _Desk:
    desk = _desk(request)
    if desk.approvals is None:
        # 503, not an empty list: an empty queue and no queue are different
        # facts, and a desk that answers "nothing waiting" when it cannot see
        # the queue is the more dangerous of the two.
        raise HTTPException(503, "this desk cannot read approvals")
    return desk


@router.get("/approvals", response_model=list[Waiting], summary="What is waiting")
async def waiting(
    request: Request, who: ident.Principal = Depends(approvals_read_guard)
) -> list[Waiting]:
    desk = _readable(request)
    assert desk.approvals is not None
    with tel.span("agent.approval.queue", **{tel.USER_ID: who.subject}) as span:
        rows = await desk.approvals.pending()
        span.set_attribute("agent.approval.depth", len(rows))
        moment = desk.now()
        return [Waiting.of(row, now=moment) for row in rows]


@router.get("/approvals/{approval_id}", response_model=Waiting, summary="One approval")
async def one(
    request: Request,
    approval_id: Annotated[str, Path(max_length=64)],
    who: ident.Principal = Depends(approvals_read_guard),
) -> Waiting:
    desk = _readable(request)
    assert desk.approvals is not None
    row = await desk.approvals.get(approval_id)
    if row is None:
        raise HTTPException(404, "no such approval")
    return Waiting.of(row, now=desk.now())


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=Decided,
    summary="Grant it or refuse it",
)
async def decide(
    request: Request,
    approval_id: Annotated[str, Path(max_length=64)],
    decision: Decision,
    who: ident.Principal = Depends(approvals_decide_guard),
) -> Decided:
    """Every refusal the workflow makes becomes a 409, not a 500."""
    desk = _desk(request)
    if desk.approver is None:
        raise HTTPException(503, "this desk cannot decide approvals")
    try:
        row = await desk.approver.decide(
            approval_id,
            granted=decision.granted,
            by=who.subject,
            by_customer=who.customer_id,
            now=desk.now(),
        )
    except ap.ApprovalError as refused:
        detail = str(refused)
        raise HTTPException(404 if "no approval" in detail else 409, detail) from None

    return Decided(
        id=row.id,
        state=row.state.value,
        granted=row.granted,
        decided_by=row.decided_by or "",
        result=row.result or "",
        waited_s=max(0, desk.now() - row.created_at),
    )


__all__ = ["Decided", "Decision", "Waiting", "router"]
