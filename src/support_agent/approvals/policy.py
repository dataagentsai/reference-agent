"""Which actions need a person, and for how long a decision stays good."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from support_agent import identity as ident

REFUND_ACTION = "issue_refund"


@dataclass(frozen=True)
class Policy:
    """What needs a human, and for how long the answer stays good."""

    refund_threshold: Decimal = Decimal("10000")
    """₹10,000, from the functional spec's P-ESCALATE."""

    remind_before_s: int = 60 * 60
    """How long before an approval expires to say it is still waiting (T-059).
    An hour: long enough for somebody to act on the reminder, short enough that
    it is about *this* approval rather than a digest of the day's. Zero turns
    reminders off, which is what a deployment with nowhere to send one wants."""

    ttl_s: int = 24 * 60 * 60
    """An approval expires. A refund authorised three days ago and executed
    today is a decision nobody actually made about today's situation — so a
    stale grant fails closed rather than falling through."""

    elevated_scope: str = ident.SCOPE_REFUNDS_WRITE

    owed_statuses: frozenset[str] = frozenset({"returned"})
    """The states in which a refund is owed, and so the agent's to issue alone.
    AOAS `issue_refund.authority.agent_when`: a person may refund in any state,
    the agent only what is owed."""


def requires_approval(order: Mapping[str, object], policy: Policy) -> str | None:
    """The reason refunding this order needs a human, or `None` when the agent
    may issue it alone — every `agent_when` condition holds.

    The amount is the order's `total` as the order system holds it (AOAS
    `issue_refund.amount_from: order.total`), never a number from the
    conversation — F-014 was a threshold compared against whatever the model
    said. A total that is missing or unreadable needs approval: a gate that
    cannot read the number must not conclude the number is small.
    """
    status = order.get("status")
    if status not in policy.owed_statuses:
        return f"the order is {status}, and a refund that is not owed needs a person"
    total = order.get("total")
    if total is None:
        return "the order's total could not be read"
    try:
        amount = Decimal(str(total))
    except (ArithmeticError, ValueError):
        return f"the order's total {total!r} could not be read"
    if not amount.is_finite():
        return f"the order's total {total!r} could not be read"
    if amount > policy.refund_threshold:
        return f"a refund of {amount} is above the {policy.refund_threshold} threshold"
    return None


__all__ = ["Policy", "REFUND_ACTION", "requires_approval"]
