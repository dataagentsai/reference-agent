"""The surface an evaluation drives.

L12 · L6 · P1. This is AHC-0010 — *a task entrypoint an evaluation can drive* —
and it is the top of the dependency graph, so it is also the composition root:
the only place that knows about every module and wires concrete implementations
into the protocols the layers below depend on.

Nothing below reaches back up. That is what makes the whole thing drivable from a
test without the test having to reconstruct the system.

The dispatch is the deterministic-first thesis made structural. Four routes,
and **only one of them reaches the model**:

    Direct   → a deterministic handler, no model call at all
    Agentic  → the loop
    Refuse   → typed refusal, free
    Escalate → typed escalation, free
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import assert_never

from support_agent import context as ctx
from support_agent import escalation as esc
from support_agent import loop as agent_loop
from support_agent import router
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.config import Budgets, RunConfig
from support_agent.contracts import (
    Agentic,
    ApprovalStore,
    CheckpointStore,
    Clock,
    Direct,
    Escalate,
    EscalationStore,
    Identity,
    LLMClient,
    Refuse,
    Refused,
    Route,
    RunId,
    ToolClient,
    TurnResult,
    new_conversation_id,
    new_run_id,
)
from support_agent.entrypoint import direct
from support_agent.entrypoint.handoff import Handoff, HandoffDesk, NoDesk
from support_agent.entrypoint.pending import ApprovalFlow, NoApprovals, PendingWork
from support_agent.entrypoint.persist import TurnPersister
from support_agent.escalation import rules as t2
from support_agent.state import Conversation, TurnNote

DEFAULT_SYSTEM_PROMPT = (
    "You are a customer support agent for a clothing retailer. "
    "Answer only from what the tools return. "
    "Never promise a delivery date, a refund amount or a policy exception that a "
    "tool has not confirmed. If you cannot do something, say so plainly."
)


@dataclass
class Agent:
    """One conversation's worth of behaviour. Constructed by `build`."""

    llm: LLMClient
    tools: ToolClient
    store: CheckpointStore
    approvals: ApprovalStore | None = None
    escalations: EscalationStore | None = None
    """Absent means the agent cannot escalate durably, and it says so rather than
    pretending: with no store, `Escalated.ticket_id` stays `None` and the reply
    promises no reference. Same honesty as `_local_tools` refusing to offer a
    refund tool when no approval store is wired."""
    deliveries: trg.DeliveryLog | None = None
    clock: Clock | None = None
    """Epoch seconds, injected. Defaults to the wall clock.

    An escalation expires, and an expiry the caller cannot control is one no
    simulation can reach: AgentTwin could drive the *desk's* moment but not the
    agent's, so the lapse path had to be tested by rewriting `expires_at` behind
    the agent's back. A seam that only one side of a conversation can see is not
    one."""
    config: RunConfig | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    budgets: Budgets = field(default_factory=Budgets)
    rules: router.Rules = field(default_factory=router.Rules)
    capacity: esc.Capacity | None = None
    """What the desk can actually absorb. `None` means unmeasured, and the agent
    then promises a reference and no time — which is true, where "a few minutes"
    would not be."""
    history_chars: int = 32_000
    """How much transcript survives a checkpoint.

    More generous than `assemble`'s window on purpose: that trim is per call and
    reversible, this one is permanent. The number is a guard against unbounded
    growth, not an attempt to be tight — and it is counted in characters rather
    than tokens because R-004 recorded that accurate counting is lost to the
    provider choice. The ruler is approximate; the bound is not."""
    tier_2: t2.RuleSet | None = None
    """State-derived escalation rules. `None` uses the defaults; an agent with no
    escalation store never reaches them at all."""

    async def handle(
        self,
        text: str,
        *,
        identity: Identity,
        conversation: Conversation | None = None,
        run_id: RunId | None = None,
        delivery_id: str | None = None,
    ) -> tuple[TurnResult, Conversation]:
        """One turn in, one typed result out.

        The conversation is returned rather than mutated, so a caller — a test,
        or a resumed approval — always holds the state that produced the result
        it is looking at.
        """
        # AAC-0076. The guard is outermost, before a run id exists: a duplicate
        # delivery must not mint a second run, because a second run gets its own
        # idempotency key space and every control below this line is scoped to
        # one run. Refusing here is the only place it can be refused.
        if self.deliveries is not None and delivery_id is not None:
            async with trg.once(self.deliveries, delivery_id):
                return await self._turn(text, identity, conversation, run_id)
        return await self._turn(text, identity, conversation, run_id)

    async def _turn(
        self,
        text: str,
        identity: Identity,
        conversation: Conversation | None,
        run_id: RunId | None,
    ) -> tuple[TurnResult, Conversation]:
        """The turn, as a sequence: gates, route, dispatch, Tier 2, record, persist."""
        run_id = run_id or new_run_id()
        conversation = conversation or Conversation(
            conversation_id=new_conversation_id(), customer_id=identity.customer_id
        )
        with tel.span("agent.turn", **self._turn_attributes(run_id, conversation, identity)):
            held, conversation = await self._gates(conversation, identity, run_id)
            if held is not None:
                return held, await self._persist(run_id, conversation)

            decision = router.route(text, rules=self.rules)
            conversation = conversation.with_messages(ctx.user_message(text))
            result = await self._dispatch(decision, conversation, identity, run_id)
            conversation = conversation.with_turn(TurnNote.of(decision, result))

            # Tier 2, after the work rather than before it. Every rule here asks
            # how the turn *went* — did the loop give up, did the tools answer, is
            # this the third time they have asked — and none of those facts exist
            # until the route has run. That is also what closes R-011: a trajectory
            # that exhausted its budget can fetch a person without the loop
            # knowing this module exists.
            escalated = await self.desk.raise_on_condition(conversation, identity, run_id, result)
            if escalated is not None:
                result = escalated

            return result, await self._persist(run_id, conversation.recording(result))

    async def _gates(
        self, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> tuple[TurnResult | None, Conversation]:
        """Work already in someone else's hands comes before anything the customer says.

        A result means the turn ends here; `None` means proceed, with the
        conversation as the gates left it.

        The escalation is checked first. A person owns this conversation, and
        nothing the customer says should start work the colleague may be about to
        make pointless. When nothing holds it any more — resolved, missing, or no
        store to read — the stale flag is cleared here rather than left to
        re-answer the same question on every future turn.
        """
        if conversation.pending_escalation_id is not None:
            held = await self.desk.hold(conversation)
            if held is not None:
                return held
            conversation = conversation.model_copy(update={"pending_escalation_id": None})

        if conversation.pending_approval_id is not None:
            resumed = await self.pending.resume(conversation, identity, run_id, self.tools)
            if resumed is not None:
                return resumed

        return None, conversation

    async def _dispatch(
        self, decision: Route, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> TurnResult:
        """Four routes, and only one of them reaches the model."""
        match decision:
            case Refuse():
                return Refused(reply=router.refusal_text(decision), reason=decision.reason)
            case Escalate():
                return await self.desk.raise_requested(decision, conversation, identity, run_id)
            case Direct():
                return await direct.answer(decision, identity, run_id, self.tools)
            case Agentic():
                result, _ = await agent_loop.run(
                    decision.goal,
                    identity=identity,
                    llm=self.llm,
                    tools=self.tools,
                    system_prompt=self.system_prompt,
                    budgets=self.budgets,
                    run_id=run_id,
                    history=conversation.messages[:-1],
                    local_tools=self.pending.offer(identity, run_id),
                )
                return result
            case _:
                assert_never(decision)

    def _turn_attributes(
        self, run_id: RunId, conversation: Conversation, identity: Identity
    ) -> dict[str, str]:
        """Standard names, so a backend groups turns into a conversation and
        attributes them to a customer with no mapping. Emitted here rather than at
        the edge because a turn reaches this point whether it arrived over HTTP or
        from a test, and a join key only some callers produce is one nothing
        downstream can rely on."""
        attributes = {
            tel.RUN_ID: run_id,
            tel.SESSION_ID: conversation.conversation_id,
            tel.USER_ID: identity.customer_id,
        }
        if self.config is not None:
            attributes[tel.CONFIG_FINGERPRINT] = self.config.fingerprint
            attributes[tel.RESOLUTION] = self.config.resolution
        return attributes

    @property
    def pending(self) -> PendingWork:
        """Read at call time, like `desk`."""
        return NoApprovals() if self.approvals is None else ApprovalFlow(self.approvals)

    @property
    def desk(self) -> Handoff:
        """Read at call time, so a store wired after construction is honoured."""
        if self.escalations is None:
            return NoDesk()
        return HandoffDesk(
            store=self.escalations,
            now=self._now,
            rules_version=self.rules.version,
            capacity=self.capacity,
            tier_2=self.tier_2,
        )

    async def _persist(self, run_id: RunId, conversation: Conversation) -> Conversation:
        return await TurnPersister(self.store, self.history_chars).persist(run_id, conversation)

    def _now(self) -> int:
        return self.clock() if self.clock is not None else int(time.time())


def build(
    *,
    llm: LLMClient,
    tools: ToolClient,
    store: CheckpointStore,
    approvals: ApprovalStore | None = None,
    escalations: EscalationStore | None = None,
    capacity: esc.Capacity | None = None,
    deliveries: trg.DeliveryLog | None = None,
    clock: Clock | None = None,
    config: RunConfig | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    history_chars: int = 32_000,
    rules: router.Rules | None = None,
    tier_2: t2.RuleSet | None = None,
) -> Agent:
    """The composition root.

    The only function in the package that knows about every layer. Everything
    else receives what it needs — which is the design half of the dependency
    rule, since an import contract cannot see a module that fetches a
    collaborator instead of being handed one.
    """
    return Agent(
        llm=llm,
        tools=tools,
        deliveries=deliveries,
        store=store,
        approvals=approvals,
        escalations=escalations,
        capacity=capacity,
        clock=clock,
        history_chars=history_chars,
        config=config,
        system_prompt=system_prompt,
        budgets=config.budgets if config else Budgets(),
        rules=rules or router.Rules(),
        tier_2=tier_2,
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "Agent", "build"]
