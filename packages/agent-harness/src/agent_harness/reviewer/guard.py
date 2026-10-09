"""Who is asking, and what they may do — shared by both desks (T-059).

One verification, one scope check, one place a route's authority is declared.
Held apart from the routes so the approvals desk and the escalation desk can
each import it without either importing the other.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from agent_harness import approvals as ap
from agent_harness import escalation as esc
from agent_harness import identity as ident
from agent_harness import telemetry as tel
from agent_harness.contracts import Approvals, Clock, Escalations


@dataclass(frozen=True)
class _Desk:
    """What one mounted desk serves from. Held on `app.state`, read per request."""

    store: Escalations
    verify: ident.Verifier
    """The identity adapter's verifier (F-30): Entra's `scp` and `roles` read as
    Entra writes them, Keycloak's as Keycloak does."""
    clock: Clock | None = None
    desk: esc.EscalationDesk | None = None
    """How this desk closes one. Absent on a read-only mount, which answers
    every question the queue asks and refuses to change anything."""
    approvals: Approvals | None = None
    """What is waiting for a decision (T-059). Absent where no approval store is
    wired, and the approvals routes then answer 503 rather than pretending the
    queue is empty — an empty queue and no queue are different facts."""
    approver: ap.ApprovalDesk | None = None
    """How this desk decides one. Held apart from `approvals` for the same
    reason `desk` is held apart from `store`: reading and deciding are different
    authorities, and a read-only mount holds only the first."""

    def now(self) -> int:
        return self.clock() if self.clock is not None else int(time.time())


def _desk(request: Request) -> _Desk:
    desk: _Desk = request.app.state.desk
    return desk


def reviewer(request: Request, authorization: str | None = Header(default=None)) -> ident.Principal:
    """Verify once, here, and hand every route a trustworthy identity.

    The 401 body is fixed for the same reason `serve`'s is: the library's own
    message is descriptive enough to tell a prober which part of the token was
    wrong. The detail goes on the span, where an operator can read it and an
    attacker cannot.
    """
    header = authorization or ""
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(401, "a bearer token is required")
    try:
        return _desk(request).verify(token)
    except ident.InvalidSession as exc:
        detail = tel.redact(f"{type(exc).__name__}: {exc}")
        with tel.span("agent.escalation.refused", **{"http.refusal_detail": detail}):
            pass
        raise HTTPException(401, "the session token is not valid") from None


def requires(scope: str) -> Callable[..., ident.Principal]:
    """One scope check, declared per route — written once, applied by the router,
    and impossible to forget on the route added next month."""

    def guard(who: ident.Principal = Depends(reviewer)) -> ident.Principal:
        if not who.may(scope):
            raise HTTPException(403, f"this session does not hold {scope}")
        return who

    return guard


# `Depends(...)` as the default rather than inside `Annotated`: `Principal` is a
# Pydantic model, and FastAPI reads a bare model-typed parameter as a request
# field — so the annotated form silently became a *query parameter named `who`*,
# and every route answered 422 before the token was ever read.


read_guard = requires(ident.SCOPE_ESCALATIONS_READ)
review_guard = requires(ident.SCOPE_ESCALATIONS_REVIEW)
approvals_read_guard = requires(ident.SCOPE_APPROVALS_READ)
approvals_decide_guard = requires(ident.SCOPE_APPROVALS_DECIDE)


__all__ = [
    "approvals_decide_guard",
    "approvals_read_guard",
    "read_guard",
    "requires",
    "review_guard",
    "reviewer",
]
