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

from support_agent import context as ctx
from support_agent import loop as agent_loop
from support_agent import router
from support_agent import telemetry as tel
from support_agent.config import Budgets, RunConfig
from support_agent.contracts import (
    Agentic,
    CheckpointStore,
    Completed,
    Direct,
    Escalate,
    Escalated,
    Failed,
    IdempotencyKey,
    Identity,
    LLMClient,
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
                    )

            conversation = _record(conversation, result)
            await self.store.checkpoint(run_id, conversation.encode())
            return result, conversation

    async def _direct(self, decision: Direct, identity: Identity, run_id: RunId) -> TurnResult:
        """A deterministic handler. No model call, and the trace says so."""
        with tel.span("agent.direct", **{"agent.handler": decision.handler}):
            key = IdempotencyKey(run_id=run_id, step=0, iteration=0)
            try:
                result = await self.tools.call("get_order", dict(decision.args), identity, key)
            except ToolUnavailable as exc:
                return Failed(
                    customer_message="I cannot reach our order system right now.",
                    detail=str(exc),
                )
            if result.is_error or not isinstance(result.structured, dict):
                return Failed(
                    customer_message="I could not find that order.",
                    detail=result.text or "no structured content",
                )
            return Completed(
                reply=ctx.render(
                    STATUS_REPLY,
                    order_id=result.structured.get("order_id", ""),
                    status=result.structured.get("status", "unknown"),
                )
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
    from support_agent.contracts import Message, NeedsApproval

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
        config=config,
        system_prompt=system_prompt,
        budgets=config.budgets if config else Budgets(),
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "Agent", "build"]
