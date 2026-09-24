"""Work a person must decide before the agent may finish it — refund approvals.

**P-REFUND** and **AHC-0057**, from the turn's side — *a refund above the
limit needs a person*, and a run may not grant itself that authority. `approvals/workflow.py`
owns getting a decision made; this owns what a *turn* does about one — offering
the request, and picking up an answer that arrived while nobody was looking.

One responsibility, behind one protocol: what the turn needs from the approval
workflow. Two questions, and the agent never branches on whether a store exists:

    offer   which harness-answered tools this turn may use to *request* approval
    resume  what became of a decision made since the last turn

The approval **returns; it never blocks**. A request checkpoints and hands back
a typed `NeedsApproval`; the next turn resumes from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from support_agent import approvals as ap
from support_agent import telemetry as tel
from support_agent.contracts import (
    Approvals,
    ApprovalState,
    Completed,
    Failed,
    IdempotencyKey,
    Identity,
    LocalTool,
    RunId,
    ToolClient,
    TurnResult,
)
from support_agent.state import Conversation

REFUND_FAILED = "The refund could not be completed."
STILL_WAITING = "That is still with a colleague to authorise."
CARRYING_OUT = "That has been authorised and is being processed now."


class PendingWork(Protocol):
    def offer(
        self,
        identity: Identity,
        run_id: RunId,
        tools: ToolClient,
        conversation_id: str = "",
    ) -> dict[str, LocalTool]: ...

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None: ...


@dataclass(frozen=True)
class NoApprovals:
    """No store wired: the agent cannot raise a refund at all — it does not fall
    back to issuing one — and there is never anything to resume."""

    def offer(
        self,
        identity: Identity,
        run_id: RunId,
        tools: ToolClient,
        conversation_id: str = "",
    ) -> dict[str, LocalTool]:
        return {}

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None:
        return None


@dataclass(frozen=True)
class ApprovalFlow:
    approvals: Approvals

    def offer(
        self,
        identity: Identity,
        run_id: RunId,
        tools: ToolClient,
        conversation_id: str = "",
    ) -> dict[str, LocalTool]:
        tool = ap.refund_tool(
            self.approvals,
            identity=identity,
            idempotency_key=IdempotencyKey(run_id=run_id, step=0, iteration=0),
            conversation_id=conversation_id,
        )
        return {tool.spec.name: tool}

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None:
        """Say what became of an approval since the last turn.

        Only says: the workflow carried out a granted refund the moment it was
        granted (T-028), so there is nothing here to execute and no clock to
        judge it by. `None` when there is nothing to resume. A still-pending
        approval short-circuits: continuing would let the customer's next
        message start work the outstanding decision may make pointless.
        """
        assert conversation.pending_approval_id is not None
        approval = await self.approvals.get(conversation.pending_approval_id)
        if approval is None:
            return None

        with tel.span("agent.approval.resume", **{"agent.approval.id": approval.id}):
            state = approval.state
            if state in (ApprovalState.ASSESSING, ApprovalState.WAITING):
                return Completed(reply=STILL_WAITING), conversation
            if state is ApprovalState.CARRYING_OUT:
                return Completed(reply=CARRYING_OUT), conversation

            cleared = conversation.model_copy(update={"pending_approval_id": None})
            if state is ApprovalState.REFUSED:
                refused = Completed(reply="A colleague reviewed this and could not authorise it.")
                return refused, cleared.recording(Completed(reply=""))
            if state is ApprovalState.EXPIRED:
                expired = Failed(
                    customer_message=(
                        "That authorisation is no longer valid — "
                        "please ask again and I will raise it afresh."
                    ),
                    detail=f"approval {approval.id} expired before anyone decided",
                )
                return expired, cleared
            if state is ApprovalState.STALE:
                # Nothing was attempted, so there is nothing half-done to
                # explain — only a decision that no longer fits what it was
                # about. Said as a fact about the order rather than as an
                # error, because from here it is neither side's mistake.
                stale = Failed(
                    customer_message=(
                        "The order changed while a colleague was reviewing this, so their "
                        "authorisation no longer matches it. Nothing has been refunded — "
                        "please ask again and I will raise it against today's details."
                    ),
                    detail=f"approval {approval.id} was granted against facts that moved: "
                    f"{approval.result or ''}",
                )
                return stale, cleared
            if state is ApprovalState.FAILED:
                return Failed(customer_message=REFUND_FAILED, detail=approval.result or ""), cleared
            done = Completed(reply="That has been authorised and the refund is on its way.")
            return done, cleared


__all__ = ["ApprovalFlow", "NoApprovals", "PendingWork"]
