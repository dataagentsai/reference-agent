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

from dataclasses import dataclass, field

from support_agent import approvals as ap
from support_agent import context as ctx
from support_agent import loop as agent_loop
from support_agent import router
from support_agent import telemetry as tel
from support_agent.config import Budgets, RunConfig
from support_agent.contracts import (
    Agentic,
    ApprovalStore,
    CheckpointStore,
    Completed,
    Direct,
    Escalate,
    Escalated,
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
from support_agent.state import Conversation

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
    config: RunConfig | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    budgets: Budgets = field(default_factory=Budgets)
    rules: router.Rules = field(default_factory=router.Rules)

    async def handle(
        self,
        text: str,
        *,
        identity: Identity,
        conversation: Conversation | None = None,
        run_id: RunId | None = None,
    ) -> tuple[TurnResult, Conversation]:
        """One turn in, one typed result out.

        The conversation is returned rather than mutated, so a caller — a test,
        or a resumed approval — always holds the state that produced the result
        it is looking at.
        """
        run_id = run_id or new_run_id()
        conversation = conversation or Conversation(
            conversation_id=new_conversation_id(),
            customer_id=identity.customer_id,
        )

        attributes = {tel.RUN_ID: run_id}
        if self.config is not None:
            attributes[tel.CONFIG_FINGERPRINT] = self.config.fingerprint
            attributes[tel.RESOLUTION] = self.config.resolution

        with tel.span("agent.turn", **attributes):
            if conversation.pending_approval_id is not None:
                resumed = await self._resume(conversation, identity, run_id)
                if resumed is not None:
                    result, conversation = resumed
                    await self.store.checkpoint(run_id, conversation.encode())
                    return result, conversation

            decision = router.route(text, rules=self.rules)
            conversation = conversation.with_messages(ctx.user_message(text))

            match decision:
                case Refuse():
                    result: TurnResult = Refused(
                        reply=_refusal_text(decision), reason=decision.reason
                    )
                case Escalate():
                    result = Escalated(
                        reply="Let me pass you to a colleague who can help with that.",
                        reason=decision.reason,
                    )
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

            conversation = _record(conversation, result)
            await self.store.checkpoint(run_id, conversation.encode())
            return result, conversation

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

            result = await self.tools.call(
                approval.action, dict(approval.args), elevated, ap.stored_key(approval)
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


def _bind(spec, args: dict[str, object]) -> dict[str, object]:
    """Map the router's arguments onto whatever the tool actually declares.

    The router knows it found an order id; it does not know what this world calls
    that field. One required string property means one place to put it.
    """
    properties = spec.input_schema.get("properties", {})
    required = [n for n in spec.input_schema.get("required", []) if n in properties]
    if len(required) == 1 and len(args) == 1:
        return {required[0]: next(iter(args.values()))}
    return dict(args)


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
    return updated


def build(
    *,
    llm: LLMClient,
    tools: ToolClient,
    store: CheckpointStore,
    approvals: ApprovalStore | None = None,
    config: RunConfig | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
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
        store=store,
        approvals=approvals,
        config=config,
        system_prompt=system_prompt,
        budgets=config.budgets if config else Budgets(),
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "Agent", "build"]
