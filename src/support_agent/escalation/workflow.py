"""Raise, resolve, lapse, sweep — each step recorded before it is spoken of."""

from __future__ import annotations

import uuid

from support_agent import telemetry as tel
from support_agent.contracts import (
    Escalation,
    EscalationOutcome,
    EscalationState,
    EscalationStore,
)

DEFAULT_TTL_S = 30 * 60
"""How long a queued escalation waits before it lapses.

Thirty minutes is a placeholder with a real shape: it should come from the
desk's actual answer time, and it is deliberately short enough that the lapse
path is exercised rather than theoretical.
"""


async def sweep(store: EscalationStore, *, now: int) -> tuple[Escalation, ...]:
    """Lapse everything nobody came for.

    The lapse path was lazy: it only ran when the customer sent another turn. So
    an escalation on a conversation somebody abandoned never expired, and sat in
    the desk's queue as permanent phantom work — the queue depth that the wait
    estimate above divides by would drift upward forever, and every promise made
    from it would get worse.

    Deliberately a plain function rather than a background thread. The caller
    decides the cadence: a periodic task in the server, one call in a test, a
    cron job in a deployment. A sweeper that owns its own scheduling is one
    nobody can drive from a scenario.
    """
    lapsed: list[Escalation] = []
    for escalation in await store.pending():
        if escalation.lapsed(now):
            lapsed.append(await lapse(store, escalation, now=now))
    return tuple(lapsed)


def new_escalation_id() -> str:
    """Short and readable, because a customer says it out loud.

    `E-` plus eight hex characters rather than a bare uuid: the id appears in the
    reply, and a reference a person cannot read back over the phone is one they
    will not use.
    """
    return f"E-{uuid.uuid4().hex[:8].upper()}"


async def raise_for(
    store: EscalationStore,
    *,
    conversation_id: str,
    run_id: str,
    customer_id: str,
    reason: str,
    rule_id: str,
    rules_version: str,
    tier: int = 1,
    ttl_s: int = DEFAULT_TTL_S,
    now: int,
) -> Escalation:
    """Write the record, then let the caller speak.

    Deliberately in this order. Everything that has gone wrong with this path
    came from telling the customer first and recording second — which is to say,
    never recording at all.
    """
    escalation = Escalation(
        id=new_escalation_id(),
        conversation_id=conversation_id,
        run_id=run_id,
        customer_id=customer_id,
        tier=tier,
        rule_id=rule_id,
        rules_version=rules_version,
        reason=reason,
        state=EscalationState.QUEUED,
        created_at=now,
        expires_at=now + ttl_s,
    )
    with tel.span(
        "agent.escalation.raise",
        **{
            tel.ESCALATION_ID: escalation.id,
            tel.ESCALATION_TIER: tier,
            tel.ESCALATION_RULE: rule_id,
            "agent.escalation.rules_version": rules_version,
        },
    ):
        await store.put(escalation)
    return escalation


class EscalationError(Exception):
    """This escalation cannot be closed that way."""


async def resolve(
    store: EscalationStore,
    escalation_id: str,
    *,
    outcome: EscalationOutcome | str,
    by: str,
    note: str = "",
    now: int,
) -> Escalation:
    """A person closes it, and says what it was.

    Three refusals, fail-closed, and each is `approvals.decide`'s reasoning in
    the shape this record has:

    **Nobody closes their own escalation.** `by` may not be the customer — the
    confused deputy of the human path, and the reason the outcome label is worth
    anything. A customer who could mark their own case resolved would make the
    over-escalation rate a number the measured party writes.

    **Closing is terminal.** Re-resolving is refused rather than overwritten. An
    outcome that can be rewritten is an outcome that can be rewritten *after*
    someone reads the dashboard.

    **A lapsed escalation cannot be resolved.** Nobody came; recording that
    somebody did, an hour later, would erase precisely the evidence the expiry
    exists to leave.

    `outcome` is required rather than defaulted. A reviewer who closes without
    saying whether the agent could have handled it has given us nothing, and
    letting that pass silently is how the false-positive rate stays unmeasurable
    forever.
    """
    # Coerced at the boundary, not trusted from it. Every caller of this is
    # across a wire or a seam — an HTTP reviewer surface, a simulated desk — and
    # none of them hands over a Python enum. `model_copy(update=...)` does not
    # validate, so an unchecked string would land in the row and surface later as
    # an outcome no dashboard has a bucket for.
    try:
        label = EscalationOutcome(outcome)
    except ValueError:
        raise EscalationError(
            f"{outcome!r} is not an outcome; expected one of {[o.value for o in EscalationOutcome]}"
        ) from None

    escalation = await store.get(escalation_id)
    if escalation is None:
        raise EscalationError(f"no escalation {escalation_id!r}")
    if not escalation.open:
        raise EscalationError(f"escalation {escalation_id!r} is already {escalation.state.value}")
    if escalation.lapsed(now):
        raise EscalationError(f"escalation {escalation_id!r} lapsed before anyone came")
    if by == escalation.customer_id:
        raise EscalationError("an escalation cannot be closed by the customer it belongs to")

    closed = escalation.model_copy(
        update={
            "state": EscalationState.RESOLVED,
            "resolved_at": now,
            "outcome": label,
            "outcome_by": by,
            "outcome_note": note or None,
        }
    )
    with tel.span(
        "agent.escalation.resolve",
        **{
            tel.ESCALATION_ID: closed.id,
            tel.ESCALATION_RULE: closed.rule_id,
            "agent.escalation.outcome": label.value,
            "agent.escalation.waited_s": now - closed.created_at,
        },
    ):
        await store.put(closed)
    return closed


async def lapse(store: EscalationStore, escalation: Escalation, *, now: int) -> Escalation:
    """Nobody came. Close it as expired and hand the conversation back."""
    expired = escalation.model_copy(update={"state": EscalationState.EXPIRED, "resolved_at": now})
    with tel.span(
        "agent.escalation.lapse",
        **{
            tel.ESCALATION_ID: expired.id,
            tel.ESCALATION_RULE: expired.rule_id,
            "agent.escalation.waited_s": now - expired.created_at,
        },
    ):
        await store.put(expired)
    return expired


__all__ = [
    "DEFAULT_TTL_S",
    "EscalationError",
    "lapse",
    "new_escalation_id",
    "raise_for",
    "resolve",
    "sweep",
]
