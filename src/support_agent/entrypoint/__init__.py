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

from support_agent import approvals as ap
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
    Completed,
    Direct,
    Escalate,
    Escalated,
    EscalationStore,
    Failed,
    IdempotencyKey,
    Identity,
    LLMClient,
    NeedsApproval,
    Refuse,
    Refused,
    RunId,
    ToolClient,
    ToolUnavailable,
    TurnResult,
    new_conversation_id,
    new_run_id,
)
from support_agent.escalation import rules as t2
from support_agent.state import Conversation, TurnNote

DEFAULT_SYSTEM_PROMPT = (
    "You are a customer support agent for a clothing retailer. "
    "Answer only from what the tools return. "
    "Never promise a delivery date, a refund amount or a policy exception that a "
    "tool has not confirmed. If you cannot do something, say so plainly."
)

LOOKUP_TOOL = "get_order"

STATUS_REPLY = (
    "Order {{ order_id }} is currently {{ status }}."
    "{% if status == 'shipped' %} It is on its way.{% endif %}"
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
    deliveries: object | None = None
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
        run_id = run_id or new_run_id()
        conversation = conversation or Conversation(
            conversation_id=new_conversation_id(),
            customer_id=identity.customer_id,
        )

        attributes = {
            tel.RUN_ID: run_id,
            # Standard names, so a backend groups turns into a conversation and
            # attributes them to a customer with no mapping. Emitted here rather
            # than at the edge because a turn reaches this point whether it
            # arrived over HTTP or from a test, and a join key that only some
            # callers produce is one nothing downstream can rely on.
            tel.SESSION_ID: conversation.conversation_id,
            tel.USER_ID: identity.customer_id,
        }
        if self.config is not None:
            attributes[tel.CONFIG_FINGERPRINT] = self.config.fingerprint
            attributes[tel.RESOLUTION] = self.config.resolution

        with tel.span("agent.turn", **attributes):
            # Checked before the approval resume, and before routing. A person
            # owns this conversation: nothing the customer says should start work
            # the colleague may be about to make pointless, and answering as
            # though no handoff happened is the exact defect this closes.
            if conversation.pending_escalation_id is not None:
                held = await self._still_with_a_colleague(conversation)
                if held is not None:
                    result, conversation = held
                    return result, await self._persist(run_id, conversation)
                # Nothing holds it any more — resolved, missing, or no store to
                # read. The flag is stale and is cleared here rather than left to
                # re-answer the same question on every future turn.
                conversation = conversation.model_copy(update={"pending_escalation_id": None})

            if conversation.pending_approval_id is not None:
                resumed = await self._resume(conversation, identity, run_id)
                if resumed is not None:
                    result, conversation = resumed
                    return result, await self._persist(run_id, conversation)

            decision = router.route(text, rules=self.rules)
            conversation = conversation.with_messages(ctx.user_message(text))

            match decision:
                case Refuse():
                    result: TurnResult = Refused(
                        reply=_refusal_text(decision), reason=decision.reason
                    )
                case Escalate():
                    result = await self._escalate(decision, conversation, identity, run_id)
                case Direct():
                    result = await self._direct(decision, identity, run_id)
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
                        local_tools=self._local_tools(identity, run_id),
                    )

            conversation = conversation.with_turn(_note(decision, result))

            # Tier 2, after the work rather than before it. Every rule here asks
            # how the turn *went* — did the loop give up, did the tools answer,
            # is this the third time they have asked — and none of those facts
            # exist until the route has run. That is also what closes R-011:
            # a trajectory that exhausted its budget can now fetch a person,
            # without the loop knowing this module exists.
            escalated = await self._tier_two(conversation, identity, run_id, result)
            if escalated is not None:
                result = escalated

            conversation = _record(conversation, result)
            return result, await self._persist(run_id, conversation)

    async def _tier_two(
        self,
        conversation: Conversation,
        identity: Identity,
        run_id: RunId,
        result: TurnResult,
    ) -> TurnResult | None:
        """Raise on a state-derived rule, or leave the turn alone.

        Returns `None` far more often than not, and deliberately never overrides
        a Tier 1 escalation: a turn that already fetched a person does not need
        a second reason to. Nor does it override `NeedsApproval` — an outstanding
        approval is a human already engaged, and replacing that with an
        escalation would discard the decision they are in the middle of making.
        """
        if self.escalations is None or isinstance(result, Escalated | NeedsApproval):
            return None

        facts = _facts(conversation)
        rule = t2.evaluate(facts, self.tier_2)
        if rule is None:
            return None

        raised = await esc.raise_for(
            self.escalations,
            conversation_id=conversation.conversation_id,
            run_id=run_id,
            customer_id=identity.customer_id,
            reason=rule.reason,
            rule_id=rule.id,
            rules_version=(self.tier_2 or t2.RuleSet()).version,
            tier=2,
            ttl_s=rule.ttl_s,
            now=self._now(),
        )
        return Escalated(
            reply=await self._handoff_text(raised.id),
            reason=rule.reason,
            ticket_id=raised.id,
            rule_id=raised.rule_id,
        )

    async def _persist(self, run_id: RunId, conversation: Conversation) -> Conversation:
        """Bound the history, write it, and hand back what was actually stored.

        Every checkpoint goes through here, which is the point: three call sites
        wrote the conversation and none of them capped it, so the one durable
        structure in the system grew without limit on every turn.

        The bounded copy is **returned**, not just written. A caller holding a
        larger history than the store does would be looking at state that no
        longer exists anywhere — and this class already promises the opposite:
        the conversation comes back so a caller always holds what produced the
        result it is looking at.
        """
        bounded = conversation.model_copy(
            update={"messages": ctx.bounded(conversation.messages, max_chars=self.history_chars)}
        )
        await self.store.checkpoint(
            run_id, bounded.encode(), conversation_id=bounded.conversation_id
        )
        return bounded

    def _now(self) -> int:
        return self.clock() if self.clock is not None else int(time.time())

    async def _escalate(
        self,
        decision: Escalate,
        conversation: Conversation,
        identity: Identity,
        run_id: RunId,
    ) -> TurnResult:
        """Write the record, then say something true about it.

        With no store the reply is deliberately weaker — no reference number,
        because there is no record to reference. An agent that invented a ticket
        id would be doing precisely what the system prompt forbids.
        """
        if self.escalations is None:
            return Escalated(
                reply="Let me pass you to a colleague who can help with that.",
                reason=decision.reason,
                rule_id=decision.rule_id,
            )

        raised = await esc.raise_for(
            self.escalations,
            conversation_id=conversation.conversation_id,
            run_id=run_id,
            customer_id=identity.customer_id,
            reason=decision.reason,
            rule_id=decision.rule_id,
            rules_version=self.rules.version,
            tier=decision.tier,
            now=self._now(),
        )
        return Escalated(
            reply=await self._handoff_text(raised.id),
            reason=decision.reason,
            ticket_id=raised.id,
            rule_id=raised.rule_id,
        )

    async def _handoff_text(self, ticket: str) -> str:
        """Say only what the queue supports.

        Three answers, and the agent is never the one choosing between them —
        the desk's state is. Closed means nobody is there and the reply says so;
        a measured desk gets a real number from depth over throughput; an
        unmeasured one gets a reference and no promise about time.
        """
        capacity = self.capacity
        if capacity is None:
            return esc.RAISED_REPLY.format(ticket=ticket)
        if not capacity.open:
            return esc.CLOSED_REPLY.format(ticket=ticket)

        depth = len(await self.escalations.pending()) if self.escalations else 0
        waiting = capacity.estimate_s(depth)
        if waiting is None:
            return esc.RAISED_REPLY.format(ticket=ticket)
        return esc.QUEUED_REPLY.format(ticket=ticket, wait=esc.humanise(waiting))

    async def _still_with_a_colleague(
        self, conversation: Conversation
    ) -> tuple[TurnResult, Conversation] | None:
        """Hold the conversation while a person owns it — or hand it back.

        Returns `None` when there is nothing to hold, so the turn proceeds
        normally. Three ways that happens, and each is a real case rather than a
        defensive branch:

        *No store.* The flag was set by an agent that had one and this one does
        not. Clearing it beats holding a conversation against a record nobody
        here can read.

        *The record is gone or already closed.* A colleague finished, or the
        store lost it. Either way the customer is served again.

        *Nobody came.* The escalation lapsed. This is the case that makes step 1
        shippable before the reviewer surface exists — without it, every
        escalated conversation would be held open forever by a queue no human can
        yet see.
        """
        assert conversation.pending_escalation_id is not None
        if self.escalations is None:
            return None

        open_now = await self.escalations.get(conversation.pending_escalation_id)
        if open_now is None or not open_now.open:
            return None

        moment = self._now()
        if open_now.lapsed(moment):
            lapsed = await esc.lapse(self.escalations, open_now, now=moment)
            handed_back = Completed(reply=esc.LAPSED_REPLY.format(ticket=lapsed.id))
            return handed_back, _record(
                conversation.model_copy(update={"pending_escalation_id": None}), handed_back
            )

        with tel.span(
            "agent.escalation.wait",
            **{
                tel.ESCALATION_ID: open_now.id,
                "agent.escalation.waited_s": moment - open_now.created_at,
            },
        ):
            return (
                Escalated(
                    reply=esc.WAITING_REPLY.format(ticket=open_now.id),
                    reason=open_now.reason,
                    ticket_id=open_now.id,
                    rule_id=open_now.rule_id,
                ),
                conversation,
            )

    def _local_tools(self, identity: Identity, run_id: RunId) -> dict[str, ap.LocalTool]:
        """Harness-answered tools. Empty when no approval store is wired, so an
        agent without one simply cannot raise a refund — it does not fall back
        to issuing one."""
        if self.approvals is None:
            return {}
        tool = ap.refund_tool(
            self.approvals,
            identity=identity,
            idempotency_key=IdempotencyKey(run_id=run_id, step=0, iteration=0),
        )
        return {tool.spec.name: tool}

    async def _resume(
        self, conversation: Conversation, identity: Identity, run_id: RunId
    ) -> tuple[TurnResult, Conversation] | None:
        """Pick up a decision made since the last turn.

        Returns `None` when there is nothing to resume, so the turn proceeds
        normally. A still-pending approval short-circuits: continuing would let
        the customer's next message start work that the outstanding decision may
        make pointless.
        """
        assert conversation.pending_approval_id is not None
        if self.approvals is None:
            return None

        approval = await self.approvals.get(conversation.pending_approval_id)
        if approval is None:
            return None

        with tel.span("agent.approval.resume", **{"agent.approval.id": approval.id}):
            if not approval.decided:
                return (
                    Completed(reply="That is still with a colleague to authorise."),
                    conversation,
                )

            cleared = conversation.model_copy(update={"pending_approval_id": None})

            if not approval.granted:
                return (
                    Completed(reply="A colleague reviewed this and could not authorise it."),
                    _record(cleared, Completed(reply="")),
                )

            try:
                elevated = ap.granted_identity(approval, identity)
            except ap.ApprovalError as exc:
                return (
                    Failed(
                        customer_message=(
                            "That authorisation is no longer valid — "
                            "please ask again and I will raise it afresh."
                        ),
                        detail=str(exc),
                    ),
                    cleared,
                )

            # F-013. The stored arguments come from the harness-local request
            # tool and the executing tool is projected from the world, so the two
            # need not agree on names — and until now nobody asked them to. The
            # registry is read with the **elevated** identity because that is the
            # only surface `issue_refund` appears on: this cannot be done at
            # request time, which is why it is done here.
            registry = await self.tools.list_tools(elevated)
            spec = registry.get(approval.action)
            if spec is None:
                return (
                    Failed(
                        customer_message="The refund could not be completed.",
                        detail=f"{approval.action} is not on the elevated surface",
                    ),
                    cleared,
                )
            try:
                arguments = _bind(spec, dict(approval.args))
            except Unbindable as exc:
                return (
                    Failed(
                        customer_message="The refund could not be completed.",
                        detail=str(exc),
                    ),
                    cleared,
                )

            result = await self.tools.call(
                approval.action, arguments, elevated, ap.stored_key(approval)
            )
            if result.is_error:
                return (
                    Failed(
                        customer_message="The refund could not be completed.",
                        detail=result.text,
                    ),
                    cleared,
                )
            return (
                Completed(reply="That has been authorised and the refund is on its way."),
                cleared,
            )

    async def _direct(self, decision: Direct, identity: Identity, run_id: RunId) -> TurnResult:
        """A deterministic handler. No model call, and the trace says so.

        The tool's argument name is read from its declared schema rather than
        assumed. An earlier version hard-coded `order_id`, which bound the
        deterministic path to one tool signature — and a world whose key field is
        `id` made it raise rather than degrade (F-005).

        Every failure below returns a typed result. The output contract has to
        hold on *this* route too, and this is the route where nobody expects a
        surprise, which is exactly why one escaped.
        """
        with tel.span("agent.direct", **{"agent.handler": decision.handler}):
            key = IdempotencyKey(run_id=run_id, step=0, iteration=0)
            try:
                registry = await self.tools.list_tools(identity)
                spec = registry.get(LOOKUP_TOOL)
                if spec is None:
                    return Failed(
                        customer_message="I cannot look that up right now.",
                        detail=f"{LOOKUP_TOOL} is not on this identity's surface",
                    )
                arguments = _bind(spec, decision.args)
                result = await self.tools.call(LOOKUP_TOOL, arguments, identity, key)
            except ToolUnavailable as exc:
                return Failed(
                    customer_message="I cannot reach our order system right now.",
                    detail=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 — the contract holds here too
                return Failed(
                    customer_message="I could not look that up.",
                    detail=f"{type(exc).__name__}: {exc}",
                )

            if result.is_error or not isinstance(result.structured, dict):
                return Failed(
                    customer_message="I could not find that order.",
                    detail=result.text or "no structured content",
                )
            return Completed(
                reply=ctx.render(
                    STATUS_REPLY,
                    order_id=result.structured.get("order_id") or result.structured.get("id", ""),
                    status=result.structured.get("status", "unknown"),
                )
            )


class Unbindable(Exception):
    """These arguments cannot be fitted to that tool's declared schema."""


def _bind(spec, args: dict[str, object]) -> dict[str, object]:
    """Map arguments onto whatever the tool actually declares.

    The caller knows it found an order id; it does not know what this world calls
    that field. One required string property means one place to put it.

    **Extended for the resume path (F-013).** The router hands over exactly one
    argument, which made the single-required/single-argument rule sufficient. An
    approval does not: it stores `{order_id, amount}` from the harness-local
    request tool, and the projected `issue_refund` declares only the entity's
    key. So two more steps, in order of confidence:

    1. **Keep what the tool declares.** An argument the schema does not mention
       is dropped rather than passed — `additionalProperties: false` would reject
       the whole call for it.
    2. **Fill a missing required slot by name.** `order_id` for a required `id`
       is the `<entity>_<key>` convention, and it is checked rather than assumed:
       the spare must equal the required name, or end with `_id`-style suffix, or
       carry it as a prefix. Exactly one candidate or nothing.

    Ambiguity raises. On the path where money moves, guessing between two spare
    values is worse than stopping — and stopping with a message beats the
    `jsonschema` exception that was surfacing through three nested task groups.
    """
    properties = spec.input_schema.get("properties", {})
    required = [n for n in spec.input_schema.get("required", []) if n in properties]
    if len(required) == 1 and len(args) == 1:
        return {required[0]: next(iter(args.values()))}

    kept = {n: v for n, v in args.items() if n in properties}
    spare = {n: v for n, v in args.items() if n not in properties}

    for name in [n for n in required if n not in kept]:
        candidates = [n for n in spare if _reads_as(n, name)]
        if len(candidates) != 1:
            raise Unbindable(
                f"{spec.name} requires {name!r} and the stored arguments "
                f"{sorted(args)} offer {candidates or 'nothing'} for it"
            )
        kept[name] = spare.pop(candidates[0])

    return kept


def _reads_as(offered: str, required: str) -> bool:
    return (
        offered == required
        or offered.endswith(f"_{required}")
        or offered.startswith(f"{required}_")
    )


def _note(decision, result: TurnResult) -> TurnNote:
    """Reduce a turn to what a rule can ask about.

    The intent is only known on a `Direct` route; `Agentic` carries candidates
    rather than a decision, and recording a guess as a fact is how a rule ends up
    counting something nobody classified.
    """
    return TurnNote(
        route=decision.kind,
        result=result.kind,
        intent=decision.intent.value if isinstance(decision, Direct) else None,
        termination=getattr(result, "termination", None),
    )


def _facts(conversation: Conversation) -> t2.Facts:
    """Turn the remembered outcomes into the numbers rules read.

    Computed rather than stored, so a rule change never needs a migration and a
    conversation written last week answers today's rules.
    """
    recent = conversation.recent
    failed = 0
    for note in reversed(recent):
        if note.result != "failed":
            break
        failed += 1

    repeated = 0
    for note in reversed(recent):
        if note.result == "completed" or note.intent is None:
            break
        if note.intent != recent[-1].intent:
            break
        repeated += 1

    return t2.Facts(
        turn_count=conversation.turn_count,
        termination=recent[-1].termination if recent else None,
        consecutive_failed=failed,
        refusals=sum(1 for n in recent if n.result == "refused"),
        repeated_intent=repeated,
        escalations=len(conversation.escalated_rules),
        already_fired=frozenset(conversation.escalated_rules),
    )


def _refusal_text(decision: Refuse) -> str:
    if decision.alternative:
        return f"I am sorry — {decision.reason}. {decision.alternative}"
    return f"I am sorry — {decision.reason}."


def _record(conversation: Conversation, result: TurnResult) -> Conversation:
    """Append what the customer was told, and remember an open approval.

    `Failed.detail` is deliberately not stored on the conversation: it is for the
    operator and lives on the span. A conversation is what the customer can be
    shown.
    """
    from support_agent.contracts import Message

    reply = getattr(result, "reply", None) or getattr(result, "customer_message", "")
    updated = conversation.with_messages(
        Message(role="assistant", content=reply, provenance="operator")
    )
    if isinstance(result, NeedsApproval):
        return updated.model_copy(update={"pending_approval_id": result.approval_id})
    # Only when there is a record to point at. An escalation with no `ticket_id`
    # is one no store accepted, and flagging the conversation against a record
    # that does not exist would hold it closed with nothing able to reopen it.
    if isinstance(result, Escalated) and result.ticket_id is not None:
        fired = updated.escalated_rules
        if result.rule_id and result.rule_id not in fired:
            fired = (*fired, result.rule_id)
        return updated.model_copy(
            update={"pending_escalation_id": result.ticket_id, "escalated_rules": fired}
        )
    return updated


def build(
    *,
    llm: LLMClient,
    tools: ToolClient,
    store: CheckpointStore,
    approvals: ApprovalStore | None = None,
    escalations: EscalationStore | None = None,
    capacity: esc.Capacity | None = None,
    deliveries: object | None = None,
    clock: Clock | None = None,
    config: RunConfig | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    history_chars: int = 32_000,
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
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "Agent", "build"]
