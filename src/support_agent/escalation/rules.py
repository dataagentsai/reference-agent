"""Tier 2 — this shop's escalations nobody asked for.

The engine — the condition vocabulary, the cooldown, the cap, the facts a rule
reads, and `evaluate` — is the harness's (`agent_harness.escalation.rules`),
re-exported here. The rules are this agent's: which conditions have earned a
customer a person, in what order, and how long each waits. `RuleSet()` here
carries them, and `evaluate` with no rule set reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_harness.escalation import rules as _engine
from agent_harness.escalation.rules import (
    MAX_PER_CONVERSATION,
    TERMINATED_BADLY,
    Condition,
    Facts,
    Tier2Rule,
    facts_of,
)

DEFAULT_RULES: tuple[Tier2Rule, ...] = (
    Tier2Rule(
        id="declined",
        when=(Condition(field="termination", equals=("declined",)),),
        reason="a refund the original payment method can no longer receive",
        priority=1,
        ttl_s=30 * 60,
    ),
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
class RuleSet(_engine.RuleSet):
    """Versioned configuration, like `router.Rules` — AAC-0101 gates changing
    these the way it gates a model change."""

    rules: tuple[Tier2Rule, ...] = field(default_factory=lambda: DEFAULT_RULES)


def evaluate(facts: Facts, rules: _engine.RuleSet | None = None) -> Tier2Rule | None:
    """The first rule that holds, or nothing — this shop's rules unless told otherwise."""
    return _engine.evaluate(facts, rules or RuleSet())


__all__ = [
    "facts_of",
    "DEFAULT_RULES",
    "MAX_PER_CONVERSATION",
    "TERMINATED_BADLY",
    "Condition",
    "Facts",
    "RuleSet",
    "Tier2Rule",
    "evaluate",
]
