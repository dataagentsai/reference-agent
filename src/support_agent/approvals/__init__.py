"""Where a person can intervene.

L14 · P6 and P8. The queue, the decision, and resumption.

**The approval returns; it never blocks.** L14's own question is what the system
does while it waits, and the answer is: it checkpoints and hands back a typed
`NeedsApproval`. Nothing is held open, nothing is promised to the customer that
the approver has not granted.

### Why not MCP elicitation

`InputRequiredResult` with `elicitation/create` is a protocol-native round trip
and was evaluated for this. It is the wrong shape here for two reasons, and both
are about who and how long:

*The approver is not the caller.* Elicitation asks the party on the other end of
the connection. A refund is authorised by an operations reviewer, not by the
customer whose refund it is.

*The wait is asynchronous and long.* Elicitation resolves inside one tool call. An
approval may take an hour, span a process restart, and be decided by someone who
was not present when it was requested.

So the decision and its audit trail are business state and live in `agent_state`.
How a resumption is *signalled* — a poll, a webhook, elicitation on a different
connection — remains a detail that can change without touching this module.

### Privilege separation

`CUSTOMER_SCOPES` deliberately excludes `refunds:write`, so an agent acting as the
customer cannot refund at all. That is not an oversight to work around: the
elevated scope exists **only** inside `granted_identity`, which refuses to mint it
without a granted, unexpired approval. The gate is not the only control; it is the
second one, and this is the first.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Approval, IdempotencyKey, Identity

REFUND_ACTION = "issue_refund"


class ApprovalError(Exception):
    """Something about this decision is not allowed."""


@dataclass(frozen=True)
class Policy:
    """What needs a human, and for how long the answer stays good."""

    refund_threshold: Decimal = Decimal("10000")
    """₹10,000, from the functional spec's P-ESCALATE."""

    ttl_s: int = 24 * 60 * 60
    """An approval expires. A refund authorised three days ago and executed
    today is a decision nobody actually made about today's situation — so a
    stale grant fails closed rather than falling through."""

    elevated_scope: str = ident.SCOPE_REFUNDS_WRITE


def requires_approval(action: str, args: dict[str, object], policy: Policy) -> str | None:
    """The reason a human is needed, or `None`.

    An unparseable amount is treated as *needing* approval. A gate that cannot
    read the number must not conclude the number is small.
    """
    if action != REFUND_ACTION:
        return None
    raw = args.get("amount")
    if raw is None:
        return "refund amount was not stated"
    try:
        amount = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return f"refund amount {raw!r} could not be read"
    if amount > policy.refund_threshold:
        return f"refund of {amount} is above the {policy.refund_threshold} threshold"
    return None


class InMemoryApprovalStore:
    durable = False

    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}
        self._lock = asyncio.Lock()

    async def put(self, approval: Approval) -> None:
        async with self._lock:
            self._items[approval.id] = approval

    async def get(self, approval_id: str) -> Approval | None:
        async with self._lock:
            return self._items.get(approval_id)

    async def pending(self) -> tuple[Approval, ...]:
        async with self._lock:
            return tuple(a for a in self._items.values() if not a.decided)


async def request(
    store: object,
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
        await store.put(approval)  # type: ignore[attr-defined]
    return approval


async def decide(
    store: object,
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
    approval = await store.get(approval_id)  # type: ignore[attr-defined]
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
        await store.put(decided)  # type: ignore[attr-defined]
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


__all__ = [
    "REFUND_ACTION",
    "ApprovalError",
    "InMemoryApprovalStore",
    "Policy",
    "decide",
    "granted_identity",
    "is_executable",
    "request",
    "requires_approval",
    "stored_key",
]
