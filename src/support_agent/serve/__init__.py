"""The HTTP edge, serving this shop's pages.

The edge — `/chat`, `/feedback`, health, the desk mounted under `/ops`, the
error contract — is the harness's (`agent_harness.serve`). The pages are this
shop's: the chat page at `/` and the desk page beside the desk's API. `build`
here is the harness's with them filled in; every other name is the harness's.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette

from agent_harness import approvals as ap
from agent_harness import escalation as esc
from agent_harness import identity as ident
from agent_harness import serve as _serve
from agent_harness.contracts import Approvals, CheckpointStore, Escalations
from agent_harness.entrypoint import TurnAgent
from agent_harness.serve import (
    MAX_BODY,
    REPLY_STATUS,
    AgentFactory,
    BadRequest,
    Inbound,
    chat,
    decode,
    health,
    page,
)
from support_agent.reviewer.page import router as desk_page
from support_agent.ui import CHAT_PAGE


def build(
    agent: TurnAgent | AgentFactory,
    *,
    issuer: ident.Issuer,
    store: CheckpointStore | None = None,
    escalations: Escalations | None = None,
    desk: esc.EscalationDesk | None = None,
    approvals: Approvals | None = None,
    approver: ap.ApprovalDesk | None = None,
) -> Starlette:
    """Wire an agent behind HTTP, with this shop's chat and desk pages."""
    return _serve.build(
        agent,
        issuer=issuer,
        chat_page=CHAT_PAGE,
        store=store,
        escalations=escalations,
        desk=desk,
        approvals=approvals,
        approver=approver,
        desk_pages=(desk_page,),
    )


def __getattr__(name: str) -> Any:
    """Every other name — private ones included — is the harness's."""
    return getattr(_serve, name)


__all__ = [
    "MAX_BODY",
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
