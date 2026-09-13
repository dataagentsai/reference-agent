"""The numbers this agent is judged on, as counters rather than as spans.

Spans answer *what happened in this run*, and they answer it well. What they
cannot answer is *what is happening* — how often this agent refuses, how often
it fetches a person, what a resolved conversation costs — because every one of
those is an aggregate over runs, and computing an aggregate from a trace store
means either sampling (so the number is wrong in a way nobody can bound) or
scanning (so nobody runs it twice).

## Why these, and not everything on a span

A counter costs almost nothing to emit and a great deal to have: it is a series
somebody will alert on, put on a wall, and quote in a meeting, and every label on
it multiplies the storage of every other label. So this file holds exactly the
numbers this agent is *judged* on, each of which somebody would notice moving:

| Number | The question it answers |
|---|---|
| turns | how much traffic, and how it ends |
| escalations | how often a person is fetched, and by which rule |
| refusals | how often the agent says no, and under which rule |
| approvals | how often money waits on somebody, and what they decided |
| unbacked promises | how often the model writes a cheque the agent cannot cash |
| spend | what a unit of work costs — the only cost figure that is a business number |
| model calls | how many, and how many produced nothing readable |

Everything else stays a span attribute, where it is available when somebody is
investigating one run and costs nothing when nobody is.

## Labels are chosen to be small and to survive

Each label here has a bounded set of values known before the run: a rule id, a
termination reason, an outcome. **Never a customer, never an order, never a free
string** — a label whose values come from the world is a series that grows
without limit, and the cardinality explosion is discovered by the bill.

`rule` is the deliberate exception worth noting: it is bounded by the rule set,
which is versioned configuration, so it grows only when somebody deliberately
adds a rule. That is the property that makes *which rule produces escalations a
human said were unnecessary* an answerable question, which is the question that
tunes the rule set.
"""

from __future__ import annotations

from opentelemetry import metrics

from support_agent.contracts import Escalated, Refused, Route, TurnResult

METER_NAME = "support_agent"

_meter = metrics.get_meter(METER_NAME)

turns = _meter.create_counter(
    "agent.turns",
    description="Turns served, by how each one ended.",
)
"""Labelled `result` — completed · refused · escalated · needs_approval · failed —
and `route`. The denominator under every rate below, so it has to be counted the
same way they are or the rates are of different things."""

escalations = _meter.create_counter(
    "agent.escalations",
    description="Conversations handed to a person, by the rule that decided.",
)
"""Labelled `rule` and `tier`. The rate itself is a product decision — too low
means the agent is overreaching, too high means it is not helping — and neither
direction is visible without the number."""

refusals = _meter.create_counter(
    "agent.refusals",
    description="Requests refused, by the rule that refused them.",
)
"""Labelled `rule`. Over-refusal is the half nobody measures: it is invisible to
anyone reading successful outputs, and users experience it as the product simply
not working for them."""

approvals = _meter.create_counter(
    "agent.approvals",
    description="Approval decisions, by what was decided.",
)
"""Labelled `outcome` — requested · granted · refused · expired. `expired` is the
one to watch: it counts money that waited on somebody who never came."""

promises = _meter.create_counter(
    "agent.unbacked_promises",
    description="Replies that committed to work nothing was doing (AHC-0106).",
)
"""Labelled `kind`. A rate that climbs says the model has found a phrasing the
prompt encourages and the system cannot honour."""

spend = _meter.create_histogram(
    "agent.spend_usd",
    unit="USD",
    description="What one unit of work spent with the provider.",
)
"""A histogram, not a counter, because the total is the least interesting thing
about it: what matters is the tail, and a sum hides an agent that succeeded in
one call and one that took forty behind the same average."""

model_calls = _meter.create_counter(
    "agent.model.calls",
    description="Calls to the provider, and how many came back unreadable.",
)
"""Labelled `outcome` — answered · malformed · unavailable. A parse-failure rate
is a number that moves when a model is swapped, and one that exists only in logs
is one nobody plots."""


def record_turn(result: TurnResult, decision: Route) -> None:
    """Every counter a turn produces, at the one point every turn passes through.

    Here rather than at each branch, because a counter incremented in five
    places is a counter that will be missed in the sixth — and the rates share
    this denominator, so a turn counted differently from a refusal makes the
    refusal rate a ratio of two different things.
    """
    turns.add(1, {"result": result.kind, "route": decision.kind})
    match result:
        case Refused():
            refusals.add(1, {"rule": result.rule_id or "unattributed"})
        case Escalated():
            escalations.add(1, {"rule": result.rule_id or "unattributed", "tier": "1"})
        case _:
            return


__all__ = [
    "METER_NAME",
    "approvals",
    "escalations",
    "model_calls",
    "promises",
    "record_turn",
    "refusals",
    "spend",
    "turns",
]
