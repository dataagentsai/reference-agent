"""The refund request tool — the one action this agent must ask a person about —
and the two activities the approval workflow runs for a refund.

The tool is the agent's side: it asks, and reports what the workflow answered.
`RefundWork` is the workflow's side: it reads the order's total, applies the
policy, and issues a granted refund. It runs in the approvals worker under that
worker's own login (T-028), so the agent never holds the path that moves money.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from temporalio import activity

from agent_harness import telemetry as tel
from support_agent.approvals.durable import ASSESS, CARRY_OUT, Ask, Assessment, CarriedOut
from support_agent.approvals.policy import (
    REFUND_ACTION,
    Policy,
    judged,
    not_requestable,
    requires_approval,
)
from support_agent.approvals.workflow import ApprovalError, carry_out, moved, stored_key
from support_agent.contracts import (
    ActionDeclined,
    Approval,
    ApprovalRequested,
    Approvals,
    ApprovalState,
    IdempotencyKey,
    Identity,
    LocalTool,
    SideEffectClass,
    ToolClient,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
    Unbindable,
    bind_arguments,
)

REFUND_WAIT_REPLY = "I have sent this to a colleague to authorise. Nothing has been refunded yet."


class RefundRequested(ApprovalRequested):
    """The model asked for a refund that needs a human. The loop sees only the
    generic `ApprovalRequested`; what the customer is told is decided here."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(approval, REFUND_WAIT_REPLY)


REQUEST_REFUND = "request_refund"

ORDER_LOOKUP = "get_order"
"""Where the amount comes from: the order system's own record of the order."""

POLICY_APPROVER = "policy:automatic-limit"
"""Who granted a refund within the limit. Every refund has an approval row that
names who authorised it — a reviewer, or this — so the audit question "who let
this money move?" has one answer for both paths."""


REQUEST_REFUND_SPEC = ToolSpec(
    name=REQUEST_REFUND,
    description=(
        "Request a refund of an order, for its full total. Small refunds are issued "
        "at once; larger ones are sent to a colleague to authorise, and you will "
        "not be told the outcome in this conversation. Tell the customer a refund "
        "was issued only if this tool says so."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}, "approval_id": {"type": "string"}},
        "required": ["status"],
    },
    side_effect=SideEffectClass.REVERSIBLE,
)
"""Harness-local. **No amount input** (F-014): the amount is the order's total, read
from the order system, so a customer who states a smaller number cannot talk a
large refund under the threshold. The description says the model may claim a
refund only on this tool's word — the gate is the control, but a model narrating
a refund that did not happen has already done the damage the gate prevents."""


def refund_tool(
    approvals: Approvals,
    *,
    identity: Identity,
    idempotency_key: IdempotencyKey,
    conversation_id: str = "",
) -> LocalTool:
    """Bind the request tool to one run's identity and key.

    The key is bound *here*, at request time, so whatever the model passes as
    arguments cannot influence which key the eventual execution runs under.

    What comes back is the workflow's answer, and the tool only reports it.
    Within the limit the refund is already issued — AOAS
    `issue_refund.authority` gives the agent that authority, and the policy
    exercises it in the workflow — so the result carries the order system's own
    words and is named for the action, which is what lets the reply say it
    happened. Above the limit, or when the total cannot be read, a person decides.
    """

    async def handle(arguments: dict[str, object]) -> ToolResult:
        # The model is shown the entity's key, `id`, as on every projected
        # tool (AOAS, input names); the approval keeps the domain's `order_id`.
        order_id = arguments.get("id")
        if not isinstance(order_id, str) or not order_id:
            return _error(REQUEST_REFUND, "an order id is required")
        approval = await approvals.request(
            action=REFUND_ACTION,
            args={"order_id": order_id},
            identity=identity,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )
        if approval.state is ApprovalState.WAITING:
            raise RefundRequested(approval)
        if approval.state is ApprovalState.DONE:
            done = {"status": "refunded", "approval_id": approval.id}
            return ToolResult(name=REFUND_ACTION, text=approval.result or "", structured=done)
        if approval.declined:
            raise ActionDeclined(DECLINED_REPLY.format(order_id=order_id), approval.result or "")
        return _error(REQUEST_REFUND, approval.result or f"the refund is {approval.state.value}")

    return LocalTool(spec=REQUEST_REFUND_SPEC, handler=handle)


DECLINED_REPLY = (
    "The refund for order {order_id} could not go back to your original payment method, "
    "which can no longer receive it."
)
"""P-REFUND-DECLINED: said first, before the handoff names who takes it on."""


def _outcome_of(result: ToolResult) -> CarriedOut:
    """What the far end's answer to a refund means, read from its kind.

    T-095 and F-089. An `allowed: false` answer is not an error on the wire, so
    it was counted a success: the approval was recorded done and the customer
    told the refund was on its way while nothing had moved. And a timeout and a
    closed card looked alike, so neither was handled as what it was.

    So: a fault that may clear — a protocol error, or `kind: transient` — is
    raised, and the workflow's retry policy tries again under the same key
    (AHC-0043, AHC-0024's bound). `kind: declined` is the payment rail refusing
    for good: never retried, and named, so a person arranges another way
    (P-REFUND-DECLINED). Any other refusal is the far end's own reason.
    """
    said = result.structured if isinstance(result.structured, dict) else {}
    kind = said.get("kind")
    if (result.is_error and result.error_channel == "protocol") or kind == "transient":
        raise ToolUnavailable(result.text or str(said.get("reason", "")))
    if result.is_error or said.get("allowed") is False:
        reason = str(said.get("reason") or result.text)
        return CarriedOut(ok=False, declined=kind == "declined", text=reason)
    return CarriedOut(ok=True, text=result.text)


def _error(name: str, text: str) -> ToolResult:
    return ToolResult(name=name, text=text, is_error=True, error_channel="execution")


@dataclass(frozen=True)
class RefundWork:
    """The refund's activities, run by the approvals worker.

    `acting_for` is that worker's own login, for the customer an approval
    names: the order is read as that customer, so an order that is not theirs
    is not found (P-OWNERSHIP), and a granted refund is issued with the approval
    named on the call for the far end to check.
    """

    tools: ToolClient
    acting_for: Callable[[str], Awaitable[Identity]]
    policy: Policy = field(default_factory=Policy)

    @activity.defn(name=ASSESS)
    async def assess(self, ask: Ask) -> Assessment:
        # Named on the read as well as on the refund: a worker with no customer
        # of its own is acting for this approval, and the far end reads whose
        # it is from the approval rather than from anything this side asserts.
        who = (await self.acting_for(ask.customer_id)).model_copy(update={"grant": ask.id})
        key = stored_key(_keyed(ask))
        order = await _order(self.tools, ask.args.get("order_id"), who, key)
        if not isinstance(order, dict):
            return Assessment(args=ask.args, reason=None, failed=order.text)
        total = order.get("total")
        args = {**ask.args, "amount": None if total is None else str(total)}
        refused = not_requestable(order, self.policy)
        if refused is not None:
            return Assessment(args=args, reason=None, failed=refused)
        reason = requires_approval(order, self.policy)
        # What the decision rests on, recorded at the moment it is read, so the
        # carry-out an hour later can tell whether it still rests on anything.
        return Assessment(
            args=args, reason=reason, approver=POLICY_APPROVER, decided_against=judged(order)
        )

    @activity.defn(name=CARRY_OUT)
    async def carry_out(self, approval: Approval) -> CarriedOut:
        who = await self.acting_for(approval.customer_id)
        # Judged at the moment the workflow scheduled this, which is the
        # workflow's clock and not this machine's.
        now = int(activity.info().current_attempt_scheduled_time.timestamp())
        with tel.span("agent.approval.carry_out", **{"agent.approval.id": approval.id}):
            changed = await self._changed(approval, who)
            if changed is not None:
                return CarriedOut(ok=False, stale=True, text=changed)
            try:
                result = await carry_out(approval, who, self.tools, policy=self.policy, now=now)
            except ApprovalError as exc:
                return CarriedOut(ok=False, text=str(exc))
        return _outcome_of(result)

    async def _changed(self, approval: Approval, who: Identity) -> str | None:
        """Re-read the order and say what has moved since it was assessed, if
        anything — F-054, and the only check on this path that looks at the
        world rather than at the record.

        **Before the grant is spent, not after.** `carry_out` mints the elevated
        identity and calls the far end in one step, so a check that ran inside
        it would be checking a refund that had already left.

        A read that fails for a *protocol* reason is raised rather than
        answered, so the activity retries under its own policy: a momentary
        blip is not news about the order, and turning one into a dead approval
        would make a person decide again for nothing. A read that fails for an
        *execution* reason — no such order, not this customer's any more — is
        the strongest possible statement that the facts moved.
        """
        reading = who.model_copy(update={"grant": approval.id})
        order = await _order(
            self.tools, approval.args.get("order_id"), reading, stored_key(approval)
        )
        if not isinstance(order, dict):
            if order.error_channel == "protocol":
                raise ToolUnavailable(order.text)
            return f"the order could not be read again: {order.text}"
        return moved(approval, judged(order))


def _keyed(ask: Ask) -> Approval:
    """Enough of a record to rebuild the key an assessment reads under."""
    return Approval(
        id=ask.id,
        action=ask.action,
        reason="",
        customer_id=ask.customer_id,
        idempotency_key=ask.idempotency_key,
        created_at=0,
        expires_at=0,
    )


async def _order(
    tools: ToolClient, order_id: object, identity: Identity, key: IdempotencyKey
) -> dict[str, object] | ToolResult:
    """The order as the order system holds it, read **as the customer** — so an
    order that is not theirs is not found (P-OWNERSHIP) — or the reason it could
    not be read, as a result the model can act on."""

    def unread(text: str, channel: Literal["execution", "protocol"]) -> ToolResult:
        return ToolResult(name=REQUEST_REFUND, text=text, is_error=True, error_channel=channel)

    if not isinstance(order_id, str) or not order_id:
        return unread("an order id is required", "execution")
    # A local tool's exceptions are not the loop's to catch — it dispatches
    # these directly — so a read that cannot happen comes back as a result.
    try:
        spec = (await tools.list_tools(identity)).get(ORDER_LOOKUP)
        if spec is None:
            return unread(f"{ORDER_LOOKUP} is not on this identity's surface", "protocol")
        arguments = bind_arguments(spec, {"order_id": order_id})
        result = await tools.call(ORDER_LOOKUP, arguments, identity, key)
    except (ToolUnavailable, Unbindable) as exc:
        return unread(str(exc), "protocol")
    if result.is_error or not isinstance(result.structured, dict):
        return unread(result.text or "that order could not be found", "execution")
    return result.structured


__all__ = [
    "RefundWork",
    "ORDER_LOOKUP",
    "POLICY_APPROVER",
    "REFUND_WAIT_REPLY",
    "REQUEST_REFUND",
    "REQUEST_REFUND_SPEC",
    "RefundRequested",
    "refund_tool",
]
