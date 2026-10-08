"""Who may decide, and the elevated identity only a granted decision mints.

**AOAS `issue_refund.authority`** — `otherwise: human_approval` is the whole of
this module: the spec says a refund the agent may not decide alone goes to a
person. The waiting is Temporal's (`approvals/durable.py`); the rules it waits
under are these, and they are ours.

The elevated scope is the part the spec does not say and the harness must:
**AHC-0057** asks that a run cannot grant itself authority over an irreversible
action, so the scope a refund needs is minted by the *decision*, never held by
the agent that asked for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agent_harness import identity as ident
from agent_harness.contracts import (
    Approval,
    ApprovalState,
    IdempotencyKey,
    Identity,
    ToolClient,
    ToolResult,
    Unbindable,
    bind_arguments,
)
from agent_harness.contracts.failures import AgentFailure, Fault


class ApprovalTerms(Protocol):
    """What the wait reads of an agent's approval policy.

    Which actions need a person, and at what threshold, are the agent's (the
    reference agent's is `support_agent.approvals.Policy`). How long a decision
    stays good, when to remind, and which scope a grant mints are what the
    workflow needs to know of it — read-only, so any frozen policy satisfies it.
    """

    @property
    def elevated_scope(self) -> str: ...
    @property
    def ttl_s(self) -> int: ...
    @property
    def remind_before_s(self) -> int: ...


@dataclass(frozen=True)
class Terms:
    """The terms a wait runs under when the caller names none."""

    remind_before_s: int = 60 * 60
    """How long before an approval expires to say it is still waiting (T-059).
    Zero turns reminders off."""
    ttl_s: int = 24 * 60 * 60
    """An approval expires: a stale grant fails closed rather than falling through."""
    elevated_scope: str = ident.SCOPE_REFUNDS_WRITE
    """The scope a granted decision mints, and nothing else does (AHC-0057)."""


class ApprovalError(AgentFailure):
    """Something about this decision is not allowed."""

    fault = Fault.REFUSED


def refusal(approval: Approval, *, by: str, by_customer: str | None = None, now: int) -> str | None:
    """Why this person may not decide this approval now, or `None`.

    Three refusals, all fail-closed, and the workflow runs them before a
    decision is accepted, so they hold whoever calls:

    **Nobody approves their own request.** `by` may not be the customer the
    approval belongs to — which is the confused deputy of the human path, and the
    control against "your colleague already approved this" (T-AD-04). `by` is
    the login and `by_customer` the customer that login is linked to, if any
    (T-002); a reviewer who is also this customer is refused under either name.

    **A decision is terminal.** Re-deciding a decided approval is refused rather
    than overwritten, because a grant that can be re-granted is a grant that can
    be executed twice.

    **An expired request cannot be decided.** It must be raised again against
    today's facts.
    """
    if approval.decided or approval.state is not ApprovalState.WAITING:
        if approval.state is ApprovalState.EXPIRED:
            return f"approval {approval.id!r} has expired"
        if approval.state is ApprovalState.ASSESSING:
            return f"approval {approval.id!r} is still being assessed"
        return f"approval {approval.id!r} was already decided"
    if now >= approval.expires_at:
        return f"approval {approval.id!r} has expired"
    if approval.customer_id in (by, by_customer):
        return "an approval cannot be granted by the customer it belongs to"
    return None


def is_executable(approval: Approval, *, now: int) -> bool:
    """Granted, and still within its window."""
    return approval.decided and approval.granted and now < approval.expires_at


def moved(approval: Approval, facts: Mapping[str, str]) -> str | None:
    """What has changed since this decision was made, or `None`.

    The expiry above asks *how long ago* a person decided; this asks *whether
    what they decided about still holds*, and the second is the one that costs
    money. A grant one minute old is within every window and worthless if the
    row it was granted against moved in that minute.

    Only the recorded fields are compared, and a field that was recorded and is
    now unreadable counts as moved — a check that cannot see the fact it is
    checking must not conclude the fact is unchanged.

    Returns the difference in words, because this ends up in front of a person
    who has to decide the same thing again and "it changed" does not tell them
    what to look at.
    """
    changed = [
        f"{field} was {was!r} and is now {facts.get(field)!r}"
        for field, was in approval.decided_against.items()
        if facts.get(field) != was
    ]
    return ", ".join(changed) if changed else None


def granted_identity(
    approval: Approval,
    base: Identity,
    *,
    policy: ApprovalTerms | None = None,
    now: int,
) -> Identity:
    """Mint the elevated identity — and only for a live grant.

    This is the single place in the system where `refunds:write` can appear. A
    pending, refused or expired approval yields nothing, so the scope cannot be
    acquired by any path that skipped the gate.
    """
    policy = policy or Terms()
    if not is_executable(approval, now=now):
        raise ApprovalError(f"approval {approval.id!r} is not executable")
    if approval.customer_id != base.customer_id:
        raise ApprovalError("approval does not belong to this identity")
    return base.model_copy(
        update={"scopes": base.scopes | {policy.elevated_scope}, "grant": approval.id}
    )


def stored_key(approval: Approval) -> IdempotencyKey:
    """Rebuild the original key. Executing under a fresh one would defeat the
    ledger and turn a double resume into a double refund."""
    run_id, step, iteration = approval.idempotency_key.rsplit(":", 2)
    from agent_harness.contracts import RunId

    return IdempotencyKey(run_id=RunId(run_id), step=int(step), iteration=int(iteration))


async def carry_out(
    approval: Approval,
    base: Identity,
    tools: ToolClient,
    *,
    policy: ApprovalTerms | None = None,
    now: int,
) -> ToolResult:
    """Execute a granted approval's action — the only path that uses the elevated
    scope, whoever granted it: a reviewer, or the policy at once. Called by the
    approval workflow, never by the agent (T-028).

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
    "ApprovalTerms",
    "Terms",
    "carry_out",
    "granted_identity",
    "is_executable",
    "moved",
    "refusal",
    "stored_key",
]
