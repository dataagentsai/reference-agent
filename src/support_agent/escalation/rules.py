"""Tier 2 — the escalations nobody asked for.

Tier 1 reads the turn's text: the customer said *"get me a manager"*, or named a
case class the agent may not settle. It runs before any work and costs nothing.

This tier reads what the **conversation has become**. A customer who has asked
the same thing four times, a trajectory that exhausted its step budget, two tool
failures in a row — none of them contain a request for a person, and every one
of them has earned one. That is the whole difference: Tier 1 answers *what did
they say*, Tier 2 answers *how is this going*.

It is also what closes **R-011 / L2×P4**. Until now escalation existed only
ahead of the loop, so a trajectory that had failed twice had no way to say *hand
this to a person* — the grid cell for control-loop escalation was empty. A rule
over `termination` fills it without the loop knowing this module exists.

## Same predicate vocabulary as the world

`Condition` is deliberately the shape `worlds/*.yaml` already uses for
`allowed_when` and `required_when` — a field, a membership list, a bound. Not
because a richer expression language would be hard, but because the omission
oracle reads that vocabulary, and a second syntax here would mean the detector
for *missed* escalations could not read the rules for *raised* ones. One
vocabulary across world invariants, omission obligations and escalation rules is
worth more than richer operators.

When a rule genuinely needs arithmetic across fields, an OR, or a quantifier
over a list, that is the signal to adopt a real expression language for all
three at once — not to bolt a second syntax onto this one.

## Why the cooldown is not optional

A Tier 1 rule fires because somebody asked, so firing again when they ask again
is correct. A Tier 2 rule fires because a *condition holds* — and lapsing an
escalation does not stop it holding. Without a cooldown, `loop-exhausted` would
raise on every subsequent failing turn, lapse, and raise again: a customer
receiving a new reference number every few minutes, and a queue filling with
duplicates of one problem.

So a rule fires at most once per conversation, and a conversation raises at most
`MAX_PER_CONVERSATION` escalations in total. Past the cap the agent stops
promising and says something true instead — which is the honest end of a bad
run, not a failure to handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_PER_CONVERSATION = 2
"""After this, stop raising. A third reference number for one unresolved problem
helps nobody and makes the queue read as three customers."""


@dataclass(frozen=True)
class Facts:
    """What the conversation has become, as numbers.

    Assembled once per evaluation and then only read. Rules are pure functions of
    this snapshot — no store, no clock, no tool — which is what makes an
    escalation reproducible from its trace: record the facts and the rule, and
    the decision replays exactly.
    """

    turn_count: int = 0
    termination: str | None = None
    """Why the loop stopped, when it ran. `None` on a route that never reached
    it, which is most of them."""
    consecutive_failed: int = 0
    refusals: int = 0
    repeated_intent: int = 0
    """How many turns in a row have carried the same unresolved intent."""
    escalations: int = 0
    already_fired: frozenset[str] = frozenset()

    def get(self, name: str) -> Any:
        return getattr(self, name, None)


@dataclass(frozen=True)
class Condition:
    """One clause. Deliberately the world file's shape.

    `equals` is a membership list rather than a scalar, matching
    `{field: status, equals: [pending, confirmed]}` — one form for "is one of"
    reads better than two forms that differ only in cardinality.
    """

    field: str
    equals: tuple[Any, ...] | None = None
    at_least: int | None = None
    at_most: int | None = None

    def holds(self, facts: Facts) -> bool:
        value = facts.get(self.field)
        if self.equals is not None and value not in self.equals:
            return False
        if self.at_least is not None and (value is None or value < self.at_least):
            return False
        return not (self.at_most is not None and (value is None or value > self.at_most))


@dataclass(frozen=True)
class Tier2Rule:
    """A rule, and what acting on it costs.

    `ttl_s` and `priority` live here rather than on the raise, because *"the
    refund is large"* and *"they asked twice"* are not the same event and should
    not wait the same length of time in the same position in the queue.
    """

    id: str
    when: tuple[Condition, ...]
    reason: str
    priority: int = 5
    ttl_s: int = 30 * 60

    def holds(self, facts: Facts) -> bool:
        return all(c.holds(facts) for c in self.when)


TERMINATED_BADLY = ("step_budget_exhausted", "cost_ceiling_reached", "oscillation_detected")

DEFAULT_RULES: tuple[Tier2Rule, ...] = (
    Tier2Rule(
        id="loop-exhausted",
        when=(Condition(field="termination", equals=TERMINATED_BADLY),),
        reason="the agent could not complete this and stopped",
        priority=2,
        ttl_s=15 * 60,
    ),
    Tier2Rule(
        id="tool-unavailable",
        when=(Condition(field="consecutive_failed", at_least=2),),
        reason="the systems needed to answer this are not responding",
        priority=2,
        ttl_s=15 * 60,
    ),
    Tier2Rule(
        id="repeated-intent",
        when=(Condition(field="repeated_intent", at_least=3),),
        reason="the customer has asked for the same thing three times without resolution",
        priority=3,
    ),
    Tier2Rule(
        id="second-refusal",
        when=(Condition(field="refusals", at_least=2),),
        reason="the agent has refused this customer twice and a person should decide",
        priority=4,
    ),
    Tier2Rule(
        id="turns-exceeded",
        when=(Condition(field="turn_count", at_least=10),),
        reason="this conversation has run long without resolving",
        priority=6,
        ttl_s=45 * 60,
    ),
)
"""Five rules, and none of them reads a word the customer wrote.

`turns-exceeded` is last on purpose. It is the weakest signal here — a long
conversation may simply be a chatty one — so it sits at the bottom of the
priority order and waits longest, and it exists mainly so AAC-0042's
turns-to-resolution budget has somewhere to be enforced.
"""


@dataclass(frozen=True)
class RuleSet:
    """Versioned configuration, like `router.Rules` — AAC-0101 gates changing
    these the way it gates a model change."""

    version: str = "t2-v1"
    rules: tuple[Tier2Rule, ...] = field(default_factory=lambda: DEFAULT_RULES)
    cap: int = MAX_PER_CONVERSATION


def evaluate(facts: Facts, rules: RuleSet | None = None) -> Tier2Rule | None:
    """The first rule that holds, or nothing.

    First match wins, so order in the rule set is part of the versioned
    configuration rather than an accident of iteration. Ordered by how
    diagnostic the signal is: a loop that exhausted its budget is a far better
    reason to fetch a person than a conversation that has merely gone on a while.

    Returns `None` — never raises, never escalates. The caller decides what to do
    with a match, exactly as `router.route` returns a decision and never calls
    the loop.
    """
    rules = rules or RuleSet()
    if facts.escalations >= rules.cap:
        return None
    for rule in rules.rules:
        if rule.id in facts.already_fired:
            # The cooldown. A Tier 2 condition does not stop holding because an
            # escalation lapsed, so without this the same rule raises, lapses and
            # raises again for as long as the conversation continues.
            continue
        if rule.holds(facts):
            return rule
    return None


__all__ = [
    "DEFAULT_RULES",
    "MAX_PER_CONVERSATION",
    "TERMINATED_BADLY",
    "Condition",
    "Facts",
    "RuleSet",
    "Tier2Rule",
    "evaluate",
]
