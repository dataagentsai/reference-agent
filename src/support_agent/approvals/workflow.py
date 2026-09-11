"""Request, decide, and the elevated identity only a granted decision mints."""

from __future__ import annotations

import time
import uuid

from support_agent import telemetry as tel
from support_agent.approvals.policy import Policy
from support_agent.contracts import (
    Approval,
    ApprovalStore,
    IdempotencyKey,
    Identity,
)


class ApprovalError(Exception):
    """Something about this decision is not allowed."""


async def request(
    store: ApprovalStore,
    *,
    action: str,
    args: dict[str, object],
    reason: str,
    identity: Identity,
    idempotency_key: IdempotencyKey,
    policy: Policy | None = None,
    now: int | None = None,
) -> Approval:
    """Record a pending decision and hand back the record.

    The idempotency key is stored, not regenerated later. That is the whole
    reason a grant an hour from now still produces one effect.
    """
    policy = policy or Policy()
    moment = now if now is not None else int(time.time())
    approval = Approval(
        id=f"apr_{uuid.uuid4().hex[:12]}",
        action=action,
        args=dict(args),
        reason=reason,
        customer_id=identity.customer_id,
        idempotency_key=idempotency_key.value,
        created_at=moment,
        expires_at=moment + policy.ttl_s,
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
    now: int | None = None,
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
    moment = now if now is not None else int(time.time())
    approval = await store.get(approval_id)
    if approval is None:
        raise ApprovalError(f"no approval {approval_id!r}")
    if approval.decided:
        raise ApprovalError(f"approval {approval_id!r} was already decided")
    if moment >= approval.expires_at:
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


def is_executable(approval: Approval, *, now: int | None = None) -> bool:
    """Granted, and still within its window."""
    moment = now if now is not None else int(time.time())
    return approval.decided and approval.granted and moment < approval.expires_at


def granted_identity(
    approval: Approval,
    base: Identity,
    *,
    policy: Policy | None = None,
    now: int | None = None,
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


__all__ = ["ApprovalError", "decide", "granted_identity", "is_executable", "request", "stored_key"]
