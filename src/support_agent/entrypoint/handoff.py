"""Handing a conversation to a person — and holding it while one owns it.

One responsibility, behind one protocol: everything the turn needs from the
escalation desk. The agent asks three questions and never branches on whether a
desk exists — `NoDesk` answers them honestly when none is wired:

    hold               is a person holding this conversation right now?
    raise_requested    the turn's text asked for one (Tier 1)
    raise_on_condition the conversation has earned one (Tier 2)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from support_agent import escalation as esc
from support_agent import telemetry as tel
from support_agent.contracts import (
    Completed,
    Escalate,
    Escalated,
    EscalationStore,
    Identity,
    NeedsApproval,
    Refused,
    RunId,
    TurnResult,
)
from support_agent.escalation import rules as t2
from support_agent.state import Conversation


class Handoff(Protocol):
    async def hold(self, conversation: Conversation) -> tuple[TurnResult, Conversation] | None: ...

    async def raise_requested(
        self, decision: Escalate, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> TurnResult: ...

    async def raise_on_condition(
        self, conversation: Conversation, identity: Identity, run_id: RunId, result: TurnResult
    ) -> TurnResult | None: ...


@dataclass(frozen=True)
class NoDesk:
    """No store wired: nothing is held, nothing is recorded — and nothing is
    promised. The capability is absent, so the request is **refused**, which is
    what the customer is told (F-024).

    It said *"let me pass you to a colleague"* and returned `Escalated` with no
    ticket. Weaker wording was not enough: the sentence claims a handover that
    no record backs, which is the failure AAC-0110 names, and the result type
    said a person had it when nobody did.
    """

    async def hold(self, conversation: Conversation) -> tuple[TurnResult, Conversation] | None:
        return None

    async def raise_requested(
        self, decision: Escalate, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> TurnResult:
        return Refused(reply=esc.NO_DESK_REPLY, reason=decision.reason)

    async def raise_on_condition(
        self, conversation: Conversation, identity: Identity, run_id: RunId, result: TurnResult
    ) -> TurnResult | None:
        return None


@dataclass(frozen=True)
class HandoffDesk:
    store: EscalationStore
    now: Callable[[], int]
    rules_version: str
    """The Tier 1 router rules' version, recorded on what they raise."""
    capacity: esc.Capacity | None = None
    """What the desk can absorb. `None` means unmeasured, and the reply then
    promises a reference and no time — which is true, where "a few minutes"
    would not be."""
    tier_2: t2.RuleSet | None = None

    async def hold(self, conversation: Conversation) -> tuple[TurnResult, Conversation] | None:
        """Hold the conversation while a person owns it — or hand it back.

        `None` when there is nothing to hold, so the turn proceeds normally: the
        record is gone or already closed (a colleague finished, or the store lost
        it), or nobody came and the escalation lapsed. The lapse is what keeps an
        escalated conversation from being held open forever by a queue no human
        can see.
        """
        assert conversation.pending_escalation_id is not None
        open_now = await self.store.get(conversation.pending_escalation_id)
        if open_now is None or not open_now.open:
            return None

        moment = self.now()
        if open_now.lapsed(moment):
            lapsed = await esc.lapse(self.store, open_now, now=moment)
            handed_back = Completed(reply=esc.LAPSED_REPLY.format(ticket=lapsed.id))
            released = conversation.model_copy(update={"pending_escalation_id": None})
            return handed_back, released.recording(handed_back)

        with tel.span(
            "agent.escalation.wait",
            **{
                tel.ESCALATION_ID: open_now.id,
                "agent.escalation.waited_s": moment - open_now.created_at,
            },
        ):
            waiting = Escalated(
                reply=esc.WAITING_REPLY.format(ticket=open_now.id),
                reason=open_now.reason,
                ticket_id=open_now.id,
                rule_id=open_now.rule_id,
            )
            return waiting, conversation

    async def raise_requested(
        self, decision: Escalate, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> TurnResult:
        """Write the record, then say something true about it — unless this
        conversation has had its share.

        `P-ESC-CAP` caps a *conversation*, and the cap lived only in the Tier 2
        evaluator: a customer who kept asking for a person got a new reference
        every time they asked, for as long as they asked (F-034). Past the cap
        the honest answer is that somebody already has this, not another number
        that looks like progress.
        """
        if conversation.escalations_raised >= t2.MAX_PER_CONVERSATION:
            return Completed(reply=esc.CAPPED_REPLY)
        raised = await esc.raise_for(
            self.store,
            conversation_id=conversation.conversation_id,
            run_id=run_id,
            customer_id=identity.customer_id,
            reason=decision.reason,
            rule_id=decision.rule_id,
            rules_version=self.rules_version,
            # What the colleague is handed: assembled from the record, never
            # summarised from the transcript (AHC-0108, AHC-0070). An escalation
            # that made the customer repeat everything is the moment an
            # assistant becomes worse than no assistant.
            context=conversation.facts.as_handoff(),
            tier=decision.tier,
            now=self.now(),
        )
        return Escalated(
            reply=await self._handoff_text(raised.id),
            reason=decision.reason,
            ticket_id=raised.id,
            rule_id=raised.rule_id,
        )

    async def raise_on_condition(
        self, conversation: Conversation, identity: Identity, run_id: RunId, result: TurnResult
    ) -> TurnResult | None:
        """Raise on a state-derived rule, or leave the turn alone.

        Never overrides a Tier 1 escalation — a turn that already fetched a
        person does not need a second reason to. Nor `NeedsApproval`: an
        outstanding approval is a human already engaged, and replacing it would
        discard the decision they are in the middle of making.
        """
        if isinstance(result, Escalated | NeedsApproval):
            return None
        rule = t2.evaluate(t2.facts_of(conversation), self.tier_2)
        if rule is None:
            return None

        raised = await esc.raise_for(
            self.store,
            conversation_id=conversation.conversation_id,
            run_id=run_id,
            customer_id=identity.customer_id,
            reason=rule.reason,
            context=conversation.facts.as_handoff(),
            rule_id=rule.id,
            rules_version=(self.tier_2 or t2.RuleSet()).version,
            tier=2,
            ttl_s=rule.ttl_s,
            now=self.now(),
        )
        return Escalated(
            reply=await self._handoff_text(raised.id),
            reason=rule.reason,
            ticket_id=raised.id,
            rule_id=raised.rule_id,
        )

    async def _handoff_text(self, ticket: str) -> str:
        """Say only what the queue supports: closed, a measured wait, or a
        reference with no promise about time. The desk's state chooses."""
        if self.capacity is None:
            return esc.RAISED_REPLY.format(ticket=ticket)
        if not self.capacity.open:
            return esc.CLOSED_REPLY.format(ticket=ticket)
        waiting = self.capacity.estimate_s(len(await self.store.pending()))
        if waiting is None:
            return esc.RAISED_REPLY.format(ticket=ticket)
        return esc.QUEUED_REPLY.format(ticket=ticket, wait=esc.humanise(waiting))


__all__ = ["Handoff", "HandoffDesk", "NoDesk"]
