"""Who may close an escalation, how it ends, and how long it waits.

The AOAS statements, as rules rather than as steps: **P-ESC-OUTCOME**, closing
records an outcome from a declared set, once; **P-ESC-LAPSE**, nobody came and
the conversation returns; **P-ESC-TTL**, a queued escalation lapses.

The steps themselves are the workflow's (`escalation/durable.py`, T-028). Raise,
hold, resolve and lapse are Temporal's, and the lapse is a timer rather than a
sweep somebody has to remember to run — which is what it was, in one process,
in a loop only the demo server had.
"""

from __future__ import annotations

import uuid

from agent_harness.contracts.failures import AgentFailure, Fault
from support_agent.contracts import Escalation, EscalationOutcome

DEFAULT_TTL_S = 30 * 60
"""How long a queued escalation waits before it lapses.

Thirty minutes is a placeholder with a real shape: it should come from the
desk's actual answer time, and it is deliberately short enough that the lapse
path is exercised rather than theoretical.
"""


def new_escalation_id() -> str:
    """Short and readable, because a customer says it out loud.

    `E-` plus eight hex characters rather than a bare uuid: the id appears in the
    reply, and a reference a person cannot read back over the phone is one they
    will not use.
    """
    return f"E-{uuid.uuid4().hex[:8].upper()}"


class EscalationError(AgentFailure):
    """This escalation cannot be closed that way."""

    fault = Fault.REFUSED


def outcome_of(outcome: EscalationOutcome | str) -> EscalationOutcome:
    """The label, or a refusal naming what was expected.

    Coerced at the boundary, not trusted from it. Every caller of this is across
    a wire or a seam — an HTTP reviewer surface, a simulated desk — and none of
    them hands over a Python enum, so an unchecked string would land in the
    record and surface later as an outcome no dashboard has a bucket for.
    """
    try:
        return EscalationOutcome(outcome)
    except ValueError:
        raise EscalationError(
            f"{outcome!r} is not an outcome; expected one of {[o.value for o in EscalationOutcome]}"
        ) from None


def refusal(
    escalation: Escalation,
    *,
    outcome: EscalationOutcome | str,
    by: str,
    by_customer: str | None = None,
    now: int,
) -> str | None:
    """Why this person may not close this escalation this way, or `None`.

    Four refusals, fail-closed, and each is `approvals.refusal`'s reasoning in
    the shape this record has:

    **The outcome must be one of the declared set.** A reviewer who closes
    without saying whether the agent could have handled it has given us nothing,
    and letting that pass silently is how the false-positive rate stays
    unmeasurable forever.

    **Nobody closes their own escalation.** `by` may not be the customer — the
    confused deputy of the human path, and the reason the outcome label is worth
    anything. A customer who could mark their own case resolved would make the
    over-escalation rate a number the measured party writes. `by` is the login
    and `by_customer` the customer it is linked to (T-002); either name is
    refused.

    **Closing is terminal.** Re-resolving is refused rather than overwritten. An
    outcome that can be rewritten is one that can be rewritten *after* somebody
    reads the dashboard.

    **A lapsed escalation cannot be resolved.** Nobody came; recording that
    somebody did, an hour later, would erase precisely the evidence the expiry
    exists to leave.
    """
    try:
        outcome_of(outcome)
    except EscalationError as exc:
        return str(exc)
    if not escalation.open:
        return f"escalation {escalation.id!r} is already {escalation.state.value}"
    if escalation.lapsed(now):
        return f"escalation {escalation.id!r} lapsed before anyone came"
    if escalation.customer_id in (by, by_customer):
        return "an escalation cannot be closed by the customer it belongs to"
    return None


__all__ = ["DEFAULT_TTL_S", "EscalationError", "new_escalation_id", "outcome_of", "refusal"]
