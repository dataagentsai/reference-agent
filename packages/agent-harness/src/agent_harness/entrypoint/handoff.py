"""Handing a conversation to a person — and holding it while one owns it.

One responsibility, behind one protocol: everything the turn needs from the
escalation desk. The agent asks three questions and never branches on whether a
desk exists — `NoDesk` answers them honestly when none is wired:

    hold               is a person holding this conversation right now?
    raise_requested    the turn's text asked for one (Tier 1)
    raise_on_condition the conversation has earned one (Tier 2)

What the customer is told at each of those moments is the agent's own voice,
handed in as `wording` and read at the moment it speaks; which conditions earn a
person is the agent's Tier 2 rule set, handed in as `tier_2`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agent_harness import telemetry as tel
from agent_harness.contracts import (
    Completed,
    Escalate,
    Escalated,
    Escalations,
    EscalationState,
    Failed,
    Identity,
    NeedsApproval,
    Refused,
    RunId,
    TerminationReason,
    TurnResult,
)
from agent_harness.escalation import rules as t2
from agent_harness.escalation.capacity import Capacity
from agent_harness.state import Conversation


class HandoffWording(Protocol):
    """Every sentence the handoff says, in the agent's voice. A module holding
    these names satisfies it, and is read at call time — so the wording is
    whatever the module says when the customer is answered."""

    RAISED_REPLY: str
    WAITING_REPLY: str
    QUEUED_REPLY: str
    CLOSED_REPLY: str
    NO_DESK_REPLY: str
    CAPPED_REPLY: str
    LAPSED_REPLY: str

    def humanise(self, seconds: int) -> str: ...


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

    wording: HandoffWording

    async def hold(self, conversation: Conversation) -> tuple[TurnResult, Conversation] | None:
        return None

    async def raise_requested(
        self, decision: Escalate, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> TurnResult:
        return Refused(reply=self.wording.NO_DESK_REPLY, reason=decision.reason)

    async def raise_on_condition(
        self, conversation: Conversation, identity: Identity, run_id: RunId, result: TurnResult
    ) -> TurnResult | None:
        return None


@dataclass(frozen=True)
class HandoffDesk:
    store: Escalations
    now: Callable[[], int]
    rules_version: str
    """The Tier 1 router rules' version, recorded on what they raise."""
    wording: HandoffWording
    """What the customer is told — read at the moment it is said."""
    capacity: Capacity | None = None
    """What the desk can absorb. `None` means unmeasured, and the reply then
    promises a reference and no time — which is true, where "a few minutes"
    would not be."""
    tier_2: t2.RuleSet = field(default_factory=t2.RuleSet)
    """The conditions that have earned a person. None by default."""

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
        if open_now is None:
            return None

        moment = self.now()
        if not open_now.open:
            # A colleague finished: the turn proceeds normally, and nothing is
            # said about a handoff that is over.
            if open_now.state is not EscalationState.EXPIRED:
                return None
            # Nobody came. The workflow's own timer closed it (T-028: this used
            # to happen only when the customer sent another turn, so a
            # conversation somebody abandoned sat in the desk's queue forever),
            # and the conversation returns with the truth — P-ESC-LAPSE.
            handed_back = Completed(reply=self.wording.LAPSED_REPLY.format(ticket=open_now.id))
            released = conversation.returned()
            return handed_back, released.recording(handed_back)

        with tel.span(
            "agent.escalation.wait",
            **{
                tel.ESCALATION_ID: open_now.id,
                "agent.escalation.waited_s": moment - open_now.created_at,
            },
        ):
            waiting = Escalated(
                reply=self.wording.WAITING_REPLY.format(ticket=open_now.id),
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
            return Completed(reply=self.wording.CAPPED_REPLY)
        raised = await self.store.raise_for(
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

        raised = await self.store.raise_for(
            conversation_id=conversation.conversation_id,
            run_id=run_id,
            customer_id=identity.customer_id,
            reason=rule.reason,
            context=conversation.facts.as_handoff(),
            rule_id=rule.id,
            rules_version=self.tier_2.version,
            tier=2,
            ttl_s=rule.ttl_s,
        )
        handoff = await self._handoff_text(raised.id)
        # A declined turn has said what happened, and the customer hears that
        # first: who takes it on is the second half (P-REFUND-DECLINED, T-095).
        if isinstance(result, Failed) and result.termination is TerminationReason.DECLINED:
            handoff = f"{result.customer_message} {handoff}"
        elif result.termination is TerminationReason.CONCERNS_UNANSWERED:
            handoff = f"{getattr(result, 'reply', '')} {handoff}".strip()
        return Escalated(
            reply=handoff,
            reason=rule.reason,
            ticket_id=raised.id,
            rule_id=raised.rule_id,
        )

    async def _handoff_text(self, ticket: str) -> str:
        """Say only what the queue supports: closed, a measured wait, or a
        reference with no promise about time. The desk's state chooses."""
        if self.capacity is None:
            return self.wording.RAISED_REPLY.format(ticket=ticket)
        if not self.capacity.open:
            return self.wording.CLOSED_REPLY.format(ticket=ticket)
        waiting = self.capacity.estimate_s(len(await self.store.pending()))
        if waiting is None:
            return self.wording.RAISED_REPLY.format(ticket=ticket)
        return self.wording.QUEUED_REPLY.format(ticket=ticket, wait=self.wording.humanise(waiting))


__all__ = ["Handoff", "HandoffDesk", "HandoffWording", "NoDesk"]
