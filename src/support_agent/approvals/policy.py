"""Which actions need a person, and for how long a decision stays good."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from support_agent import identity as ident

REFUND_ACTION = "issue_refund"


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


__all__ = ["Policy", "REFUND_ACTION", "requires_approval"]
