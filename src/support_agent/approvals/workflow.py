"""Request, decide, and the elevated identity only a granted decision mints."""

from __future__ import annotations

import uuid

from support_agent import telemetry as tel
from support_agent.approvals.policy import Policy
from support_agent.contracts import (
    Approval,
    ApprovalStore,
    IdempotencyKey,
    Identity,
    ToolClient,
    ToolResult,
    Unbindable,
    bind_arguments,
)
from support_agent.contracts.failures import AgentFailure, Fault


class ApprovalError(AgentFailure):
    """Something about this decision is not allowed."""

    fault = Fault.REFUSED


async def request(
    store: ApprovalStore,
    *,
    action: str,
    args: dict[str, object],
    reason: str,
    identity: Identity,
    idempotency_key: IdempotencyKey,
    policy: Policy | None = None,
    now: int,
) -> Approval:
    """Record a pending decision and hand back the record.

    The idempotency key is stored, not regenerated later. That is the whole
    reason a grant an hour from now still produces one effect.
    """
    policy = policy or Policy()
    approval = Approval(
        id=f"apr_{uuid.uuid4().hex[:12]}",
        action=action,
        args=dict(args),
        reason=reason,
        customer_id=identity.customer_id,
        idempotency_key=idempotency_key.value,
        created_at=now,
        expires_at=now + policy.ttl_s,
    )
    with tel.span(
        "agent.approval.request",
        **{"agent.approval.id": approval.id, "agent.approval.action": action},
    ):
        await store.put(approval)
    return approval


async def decide(
    store: ApprovalStore,
    approval_id: str,
    *,
    granted: bool,
    by: str,
    now: int,
) -> Approval:
    """A reviewer answers. Three refusals, all fail-closed.

    **Nobody approves their own request.** `by` may not be the customer the
    approval belongs to — which is the confused deputy of the human path, and the
    control against "your colleague already approved this" (T-AD-04). The claim
    would have to be true in the store, and the store is not persuadable.

    **A decision is terminal.** Re-deciding a decided approval is refused rather
    than overwritten, because a grant that can be re-granted is a grant that can
    be executed twice.

    **An expired request cannot be decided.** It must be raised again against
    today's facts.
    """
    approval = await store.get(approval_id)
    if approval is None:
        raise ApprovalError(f"no approval {approval_id!r}")
    if approval.decided:
        raise ApprovalError(f"approval {approval_id!r} was already decided")
    if now >= approval.expires_at:
        raise ApprovalError(f"approval {approval_id!r} has expired")
    if by == approval.customer_id:
        raise ApprovalError("an approval cannot be granted by the customer it belongs to")

    decided = approval.model_copy(update={"decided": True, "granted": granted, "decided_by": by})
    with tel.span(
        "agent.approval.decide",
        **{"agent.approval.id": approval_id, "agent.approval.granted": granted},
    ):
        await store.put(decided)
    return decided


def is_executable(approval: Approval, *, now: int) -> bool:
    """Granted, and still within its window."""
    return approval.decided and approval.granted and now < approval.expires_at


def granted_identity(
    approval: Approval,
    base: Identity,
    *,
    policy: Policy | None = None,
    now: int,
) -> Identity:
    """Mint the elevated identity — and only for a live grant.

    This is the single place in the system where `refunds:write` can appear. A
    pending, refused or expired approval yields nothing, so the scope cannot be
    acquired by any path that skipped the gate.
    """
    policy = policy or Policy()
    if not is_executable(approval, now=now):
        raise ApprovalError(f"approval {approval.id!r} is not executable")
    if approval.customer_id != base.customer_id:
        raise ApprovalError("approval does not belong to this identity")
    return base.model_copy(update={"scopes": base.scopes | {policy.elevated_scope}})


def stored_key(approval: Approval) -> IdempotencyKey:
    """Rebuild the original key. Executing under a fresh one would defeat the
    ledger and turn a double resume into a double refund."""
    run_id, step, iteration = approval.idempotency_key.rsplit(":", 2)
    from support_agent.contracts import RunId

    return IdempotencyKey(run_id=RunId(run_id), step=int(step), iteration=int(iteration))


async def carry_out(
    approval: Approval,
    base: Identity,
    tools: ToolClient,
    *,
    policy: Policy | None = None,
    now: int,
) -> ToolResult:
    """Execute a granted approval's action — the only path that uses the elevated
    scope, whoever granted it: a reviewer on a later turn, or the policy at once.

    Raises `ApprovalError` when the grant is not executable; every other failure
    is the result, so the caller decides what the customer is told.

    F-013. The stored arguments come from the harness-local request tool and the
    executing tool is projected from the world, so the two need not agree on
    names. The registry is read with the **elevated** identity because that is
    the only surface the action appears on.
    """
    elevated = granted_identity(approval, base, policy=policy, now=now)
    registry = await tools.list_tools(elevated)
    spec = registry.get(approval.action)
    if spec is None:
        missing = f"{approval.action} is not on the elevated surface"
        return ToolResult(
            name=approval.action, text=missing, is_error=True, error_channel="protocol"
        )
    try:
        arguments = bind_arguments(spec, dict(approval.args))
    except Unbindable as exc:
        return ToolResult(
            name=approval.action, text=str(exc), is_error=True, error_channel="protocol"
        )
    return await tools.call(approval.action, arguments, elevated, stored_key(approval))


__all__ = [
    "ApprovalError",
    "carry_out",
    "decide",
    "granted_identity",
    "is_executable",
    "request",
    "stored_key",
]
