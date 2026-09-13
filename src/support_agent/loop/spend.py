"""Tokens, and what they cost.

Two things that look like one. **Tokens are always counted**, because a run
whose usage is unknown cannot be compared with any other run and the number is
free — the provider already sent it. **Money is counted only where a meter is
wired**, because pricing is a deployment's business and an agent that invented a
price would be reporting a number nobody could reconcile with an invoice.

The split matters when a meter is absent: usage still accumulates, spend stays
zero, and nothing pretends otherwise. A system that reported a spend of zero
because it had no price table would be reporting a cheaper system than the one
running (AHC-0101), which is the failure that makes every ceiling above it hold
and every budget alert stay quiet.
"""

from __future__ import annotations

from support_agent import telemetry as tel
from support_agent.contracts import Usage
from support_agent.cost import Meter


def account(
    running: Usage, call: Usage, *, meter: Meter | None
) -> tuple[Usage, float | None, float]:
    """The run's usage after this call, its spend so far, and what this call cost.

    `None` for the spend says *not measured*, which a caller must be able to
    tell from zero — the first is a deployment that did not wire a meter and the
    second is a run that has not cost anything yet.

    The per-call figure stays with the caller, which holds the span: the ceiling
    is enforced between calls, so the number that explains a stop is the one
    from the call that crossed it, and it belongs on that call's span rather
    than on the run's.
    """
    total = Usage(
        input_tokens=running.input_tokens + call.input_tokens,
        output_tokens=running.output_tokens + call.output_tokens,
    )
    if meter is None:
        return total, None, 0.0

    cost = float(meter.record(call))
    spent = meter.as_usd()
    tel.counters.spend.record(spent)
    return total, spent, cost


__all__ = ["account"]
