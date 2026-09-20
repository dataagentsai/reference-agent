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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import assert_never

from support_agent import binding, router
from support_agent import context as ctx
from support_agent import escalation as esc
from support_agent import loop as agent_loop
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.config import Budgets, RunConfig
from support_agent.contracts import (
    Agentic,
    Approvals,
    CheckpointStore,
    Clock,
    Completed,
    Direct,
    Escalate,
    Escalations,
    Failed,
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
from support_agent.cost import Meter
from support_agent.entrypoint import consent, direct, ending, promise
from support_agent.entrypoint.handoff import Handoff, HandoffDesk, NoDesk
from support_agent.entrypoint.opening import opening
from support_agent.entrypoint.pending import ApprovalFlow, NoApprovals, PendingWork
from support_agent.entrypoint.persist import TurnPersister, agree_on_durability
from support_agent.escalation import rules as t2
from support_agent.state import Conversation, TurnNote, facts

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
    approvals: Approvals | None = None
    escalations: Escalations | None = None
    """Absent means the agent cannot escalate at all, and it says so rather than
    pretending: with no store the request is refused and nothing is claimed.
    Same honesty as the approval flow refusing to offer a refund tool when no
    approval store is wired."""
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
    policy_rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None = None
    fresh_for_s: int | None = None
    """How long a read of a row stays usable before an irreversible action may
    rely on it (AHC-0107). The binding's number; `None` turns the mechanism off,
    which is right for a deployment whose world has one writer."""
    """The guardrails, per position. `None` uses the defaults. Until F-027 this
    could not be set from here at all: the loop took the parameter and the
    composition root never passed it, so the only rules that could ever run were
    the built-in ones."""
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
    synthetic_customers: frozenset[str] = frozenset()
    """Customers who are the canary (AHC-0113). Their turns are marked, and every
    rate excludes them; nothing else about them is different."""
    metering: Callable[[], Meter] | None = None
    """A fresh meter per task, so AOAS `Q-COST` — spend per task at most the
    configured ceiling — can stop a run. `None` means no priced model is
    configured, and the step budget is the only bound (F-019: it used to be
    `None` on every path, so the ceiling was configured and never reachable)."""

    async def opening(self, identity: Identity) -> str:
        """What the customer is shown on opening a conversation. No model (P-OPEN)."""
        return await opening(
            identity, tools=self.tools, approvals=self.approvals, escalations=self.escalations
        )

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
        started = time.monotonic()
        synthetic = identity.customer_id in self.synthetic_customers
        attributes = ending.opened(run_id, conversation, identity, self.config, synthetic)
        with tel.span("agent.turn", **attributes) as turn_span:
            tel.set_payload(turn_span, tel.INPUT, text)
            held, conversation = await self._gates(conversation, identity, run_id)
            if held is not None:
                ending.closed(turn_span, held, None, started, synthetic)
                return held, await self._persist(run_id, conversation)

            decision = router.route(text, rules=self.rules)
            # The customer's own words, not the model's reading of them
            # (AHC-0108). Recorded before anything runs, so a turn that fails
            # still leaves a record of what was being attempted.
            conversation = conversation.model_copy(
                update={"facts": conversation.facts.asking(text)}
            ).with_messages(ctx.user_message(text))
            identity = consent.granting(identity, conversation, text, self.rules)
            result, landed, tried = await self._dispatch(decision, conversation, identity, run_id)
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

            after = consent.pending(
                facts.after(conversation.facts, result, landed), tried, identity
            )
            conversation = conversation.model_copy(update={"facts": after})
            result = await promise.honest(result, self.desk, conversation, identity, run_id)
            result = _screened(result, identity, self.policy_rules)
            ending.closed(turn_span, result, decision, started, synthetic)
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
    ) -> tuple[TurnResult, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        """Four routes, and only one of them reaches the model.

        Returns the effects the far system confirmed as well as the result,
        because only this function ever sees them and the record they go into
        (AHC-0108) is the caller's. Three of the four routes confirm nothing:
        two never act, and the deterministic one never writes (`P-DIRECT-READS`).
        """
        match decision:
            case Refuse():
                return (
                    Refused(
                        reply=router.refusal_text(decision),
                        reason=decision.reason,
                        rule_id=decision.rule_id,
                    ),
                    (),
                    (),
                )
            case Escalate():
                held = await self.desk.raise_requested(decision, conversation, identity, run_id)
                return held, (), ()
            case Direct():
                return await direct.answer(decision, identity, run_id, self.tools), (), ()
            case Agentic():
                result, trace = await agent_loop.run(
                    decision.goal,
                    identity=identity,
                    llm=self.llm,
                    tools=self.tools,
                    system_prompt=self.system_prompt,
                    budgets=self.budgets,
                    run_id=run_id,
                    history=conversation.messages[:-1],
                    local_tools=self.pending.offer(
                        identity, run_id, self.tools, conversation.conversation_id
                    ),
                    policy_rules=self.policy_rules,
                    meter=self.metering() if self.metering is not None else None,
                    now=self._now,
                    fresh_for_s=self.fresh_for_s,
                )
                return result, tuple(trace.effects), tuple(trace.tool_calls)
            case _:
                assert_never(decision)

    @property
    def pending(self) -> PendingWork:
        """Read at call time, like `desk`."""
        if self.approvals is None:
            return NoApprovals()
        return ApprovalFlow(self.approvals)

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


def _screened(
    result: TurnResult,
    identity: Identity,
    rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None = None,
) -> TurnResult:
    """Every reply passes the reply guardrails before the customer reads it —
    whichever route produced it (F-020). One point, so the next template edit on
    any route is screened without anyone remembering to screen it. The verdict
    and the rule that fired are on the `agent.policy` span `enforce` opens.

    The agent's **configured** rules, not only the built-in ones: this read the
    defaults whatever it was given, so a deployment that added a reply rule was
    screened by the rules it had not configured (F-027)."""
    text = result.customer_message if isinstance(result, Failed) else result.reply
    verdict = pol.enforce(
        pol.Context(position=pol.Position.REPLY, identity=identity, text=text),
        None if rules is None else rules.get(pol.Position.REPLY),
    )
    if not verdict.blocked:
        return result
    # The words are replaced; what the turn *did* is not. An escalation that was
    # raised stays raised and an approval stays pending — replacing the result
    # type would drop the handoff the reply was about.
    if isinstance(result, Failed):
        return result.model_copy(update={"customer_message": pol.SAFE_REPLY})
    if isinstance(result, Completed):
        # A completion carries nothing but its words, and the words were
        # refused — so the result is a refusal. It used to stay `Completed`
        # with a `REFUSED` termination, which a caller branching on the type
        # read as a success (F-026, AHC-0017).
        return Refused(reply=pol.SAFE_REPLY, reason=verdict.reason, rule_id=verdict.rule)
    return result.model_copy(update={"reply": pol.SAFE_REPLY})


def build(
    *,
    llm: LLMClient,
    tools: ToolClient,
    store: CheckpointStore,
    approvals: Approvals | None = None,
    escalations: Escalations | None = None,
    capacity: esc.Capacity | None = None,
    deliveries: trg.DeliveryLog | None = None,
    clock: Clock | None = None,
    config: RunConfig | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    history_chars: int = 32_000,
    rules: router.Rules | None = None,
    policy_rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None = None,
    tier_2: t2.RuleSet | None = None,
    metering: Callable[[], Meter] | None = None,
    fresh_for_s: int | None = binding.FRESH_FOR_S,
    synthetic_customers: frozenset[str] = frozenset(),
) -> Agent:
    """The composition root.

    The only function in the package that knows about every layer. Everything
    else receives what it needs — which is the design half of the dependency
    rule, since an import contract cannot see a module that fetches a
    collaborator instead of being handed one.
    """
    agree_on_durability(
        conversation=store, approvals=approvals, escalations=escalations, deliveries=deliveries
    )
    if metering is None and config is not None:
        metering = partial(Meter, config.model, ceiling_usd=config.budgets.max_cost_usd)
    if metering is not None:
        metering()  # an unpriced model fails here, at startup — never mid-conversation
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
        policy_rules=policy_rules,
        tier_2=tier_2,
        metering=metering,
        fresh_for_s=fresh_for_s,
        synthetic_customers=synthetic_customers,
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "Agent", "build"]
