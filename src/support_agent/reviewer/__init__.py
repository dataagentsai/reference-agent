"""The reviewer surface, with this shop's desk page.

The desk's API — the escalation queue, closing a row, the approvals a person
decides, the scoped guards — is the harness's (`agent_harness.reviewer`). The
page a colleague works from is this shop's (`reviewer.page`, in this shop's
words). `build` here is the harness's with that page mounted; every other name
is the harness's.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from agent_harness import approvals as ap
from agent_harness import escalation as esc
from agent_harness import identity as ident
from agent_harness import reviewer as _reviewer
from agent_harness.contracts import Approvals, Clock, Escalations
from agent_harness.reviewer import Closed, Queued, Resolution
from support_agent.reviewer.page import router as desk_page


def build(
    store: Escalations,
    *,
    issuer: ident.Issuer,
    clock: Clock | None = None,
    desk: esc.EscalationDesk | None = None,
    approvals: Approvals | None = None,
    approver: ap.ApprovalDesk | None = None,
) -> FastAPI:
    """The reviewer app, ready to mount, with this shop's desk page."""
    return _reviewer.build(
        store,
        verify=ident.verifier(issuer),
        clock=clock,
        desk=desk,
        approvals=approvals,
        approver=approver,
        pages=(desk_page,),
    )


def __getattr__(name: str) -> Any:
    """Every other name — private ones included — is the harness's."""
    return getattr(_reviewer, name)


__all__ = ["Closed", "Queued", "Resolution", "build"]
