"""The refund request tool — the one action this agent must ask a person about."""

from __future__ import annotations

from support_agent.approvals.policy import REFUND_ACTION, Policy, requires_approval
from support_agent.approvals.workflow import request
from support_agent.contracts import (
    Approval,
    ApprovalRequested,
    ApprovalStore,
    IdempotencyKey,
    Identity,
    LocalTool,
    SideEffectClass,
    ToolResult,
    ToolSpec,
)

REFUND_WAIT_REPLY = "I have sent this to a colleague to authorise. Nothing has been refunded yet."


class RefundRequested(ApprovalRequested):
    """The model asked for a refund that needs a human. The loop sees only the
    generic `ApprovalRequested`; what the customer is told is decided here."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(approval, REFUND_WAIT_REPLY)


REQUEST_REFUND = "request_refund"


REQUEST_REFUND_SPEC = ToolSpec(
    name=REQUEST_REFUND,
    description=(
        "Request a refund for an order. Refunds above the approval threshold are "
        "sent to a colleague to authorise; you will not be told the outcome in "
        "this conversation. Never tell the customer a refund has been issued."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "amount": {"type": "string", "description": "Amount in INR"},
        },
        "required": ["order_id", "amount"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}, "approval_id": {"type": "string"}},
        "required": ["status"],
    },
    side_effect=SideEffectClass.REVERSIBLE,
)
"""Harness-local. The description says outright that the model must not claim a
refund happened — the gate is the control, but a model narrating a granted
refund to the customer has already done the damage the gate exists to prevent."""


def refund_tool(
    store: ApprovalStore,
    *,
    identity: Identity,
    idempotency_key: IdempotencyKey,
    policy: Policy | None = None,
    now: int,
) -> LocalTool:
    """Bind the request tool to one run's identity and key.

    The key is bound *here*, at request time, so whatever the model passes as
    arguments cannot influence which key the eventual execution runs under.
    """
    policy = policy or Policy()

    async def handle(arguments: dict[str, object]) -> ToolResult:
        reason = requires_approval(REFUND_ACTION, arguments, policy)
        if reason is None:
            return ToolResult(
                name=REQUEST_REFUND,
                structured={"status": "below_threshold"},
                text="within the automatic limit",
            )
        approval = await request(
            store,
            action=REFUND_ACTION,
            args=arguments,
            reason=reason,
            identity=identity,
            idempotency_key=idempotency_key,
            policy=policy,
            now=now,
        )
        raise RefundRequested(approval)

    return LocalTool(spec=REQUEST_REFUND_SPEC, handler=handle)


__all__ = [
    "REFUND_WAIT_REPLY",
    "REQUEST_REFUND",
    "REQUEST_REFUND_SPEC",
    "RefundRequested",
    "refund_tool",
]
