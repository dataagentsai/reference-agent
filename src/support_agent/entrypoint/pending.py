"""Work a person must decide before the agent may finish it — refund approvals.

One responsibility, behind one protocol: what the turn needs from the approval
workflow. Two questions, and the agent never branches on whether a store exists:

    offer   which harness-answered tools this turn may use to *request* approval
    resume  a decision made since the last turn, carried out or reported

The approval **returns; it never blocks**. A request checkpoints and hands back
a typed `NeedsApproval`; the next turn resumes from here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from support_agent import approvals as ap
from support_agent import telemetry as tel
from support_agent.contracts import (
    ApprovalStore,
    Completed,
    Failed,
    IdempotencyKey,
    Identity,
    LocalTool,
    RunId,
    ToolClient,
    TurnResult,
    Unbindable,
    bind_arguments,
)
from support_agent.state import Conversation

REFUND_FAILED = "The refund could not be completed."


class PendingWork(Protocol):
    def offer(self, identity: Identity, run_id: RunId) -> dict[str, LocalTool]: ...

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None: ...


@dataclass(frozen=True)
class NoApprovals:
    """No store wired: the agent cannot raise a refund at all — it does not fall
    back to issuing one — and there is never anything to resume."""

    def offer(self, identity: Identity, run_id: RunId) -> dict[str, LocalTool]:
        return {}

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None:
        return None


@dataclass(frozen=True)
class ApprovalFlow:
    store: ApprovalStore
    now: Callable[[], int]
    """The agent's clock — the only time this flow reads. An approval minted or
    checked against the wall clock while everything else reads an injected one
    gets a different expiry on every replay of the same run (F-021)."""

    def offer(self, identity: Identity, run_id: RunId) -> dict[str, LocalTool]:
        tool = ap.refund_tool(
            self.store,
            identity=identity,
            idempotency_key=IdempotencyKey(run_id=run_id, step=0, iteration=0),
            now=self.now(),
        )
        return {tool.spec.name: tool}

    async def resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId, tools: ToolClient
    ) -> tuple[TurnResult, Conversation] | None:
        """Pick up a decision made since the last turn.

        `None` when there is nothing to resume. A still-pending approval
        short-circuits: continuing would let the customer's next message start
        work the outstanding decision may make pointless.
        """
        assert conversation.pending_approval_id is not None
        approval = await self.store.get(conversation.pending_approval_id)
        if approval is None:
            return None

        with tel.span("agent.approval.resume", **{"agent.approval.id": approval.id}):
            if not approval.decided:
                return Completed(reply="That is still with a colleague to authorise."), conversation

            cleared = conversation.model_copy(update={"pending_approval_id": None})
            if not approval.granted:
                refused = Completed(reply="A colleague reviewed this and could not authorise it.")
                return refused, cleared.recording(Completed(reply=""))

            try:
                elevated = ap.granted_identity(approval, identity, now=self.now())
            except ap.ApprovalError as exc:
                expired = Failed(
                    customer_message=(
                        "That authorisation is no longer valid — "
                        "please ask again and I will raise it afresh."
                    ),
                    detail=str(exc),
                )
                return expired, cleared

            # F-013. The stored arguments come from the harness-local request tool
            # and the executing tool is projected from the world, so the two need
            # not agree on names. The registry is read with the **elevated**
            # identity because that is the only surface the action appears on.
            registry = await tools.list_tools(elevated)
            spec = registry.get(approval.action)
            if spec is None:
                missing = f"{approval.action} is not on the elevated surface"
                return Failed(customer_message=REFUND_FAILED, detail=missing), cleared
            try:
                arguments = bind_arguments(spec, dict(approval.args))
            except Unbindable as exc:
                return Failed(customer_message=REFUND_FAILED, detail=str(exc)), cleared

            result = await tools.call(approval.action, arguments, elevated, ap.stored_key(approval))
            if result.is_error:
                return Failed(customer_message=REFUND_FAILED, detail=result.text), cleared
            done = Completed(reply="That has been authorised and the refund is on its way.")
            return done, cleared


__all__ = ["ApprovalFlow", "NoApprovals", "PendingWork"]
