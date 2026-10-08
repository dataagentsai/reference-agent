"""Which actions need a person, and for how long a decision stays good."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from agent_harness import identity as ident

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

    requestable_statuses: frozenset[str] = frozenset({"delivered", "returned", "cancelled"})
    """The states in which a refund may be *requested* at all — AOAS
    `request_refund.preconditions`, added after generation run 1. Before it, a
    note planted in a shipped order could put a refund request in front of a
    reviewer: nothing moved, but an instruction the customer never gave reached
    a person's queue. Before delivery the answer is a cancellation; a parcel
    lost in transit is a handoff (the operation's `on_refusal`)."""


JUDGED = ("status", "total")
"""The fields a refund decision depends on — and so the fields that invalidate
it if they move (F-054).

Exactly what `requires_approval` reads below, and exactly what `amount_from`
takes. It is a deliberately short list: `days_since_delivery` moves on its own
every midnight and no decision here rests on it, so including it would expire
grants for the passage of time and teach everyone to ignore the control.
"""


def judged(order: Mapping[str, object]) -> dict[str, str]:
    """The facts a decision about this order rests on, ready to compare later.

    One function, called on both sides of the wait — by the assessment that
    records them and by the carry-out that re-reads them. Two functions here
    would be two chances for the recording and the check to drift apart, and
    the drift would show as a control that silently passes.
    """
    return {field: str(order.get(field)) for field in JUDGED}


def not_requestable(order: Mapping[str, object], policy: Policy) -> str | None:
    """Why a refund cannot even be requested for this order, or `None`.

    A refusal, not a question for a person: the request never reaches a
    reviewer. Checked before `requires_approval`, because an order nobody may
    request a refund for has no approval to need.
    """
    status = order.get("status")
    if status in policy.requestable_statuses:
        return None
    return (
        f"the order is {status}; a refund can be requested once it is delivered, "
        "returned or cancelled — before delivery, cancel it instead while that is still possible"
    )


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


__all__ = ["JUDGED", "Policy", "REFUND_ACTION", "judged", "not_requestable", "requires_approval"]
