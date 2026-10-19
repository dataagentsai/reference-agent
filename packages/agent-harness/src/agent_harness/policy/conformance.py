"""The `policy` port's conformance cases, one per invariant key (T-048, tier 2).

Two realizations are the same port because both pass these, not because both
are one class. A case names what it checks as `policy/<key>`, the key AHC's
`ports/policy.yaml` gives the invariant, and takes a `Make`: given a rule table
and a time budget, return something that answers the port. Our loop's
realization is `RulePolicy`; a framework's adapter supplies its own `Make`,
which builds the rules into that framework's extension point and answers
through it.

The cases are pinned. *Unchanged* means this file byte for byte: a case edited
so an adapter passes is a failure of the adapter, whatever the edit
(ai-harness-catalog `docs/PORTS-AS-A-STANDARD.md`). `DIGEST` in the tests holds
the pin.

Static invariants are checked too where a run can show them: configuration's
order deciding which of two blocking rules speaks, and each side running only
its own rules. The inspection rule stays the authority for those.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import jsonschema

from agent_harness.contracts import Identity
from agent_harness.policy import Rule
from agent_harness.policy.port import DECISION_SCHEMA, Policy, decision
from agent_harness.policy.verdicts import ALLOW, Context, Position, Verdict, block

Make = Callable[[Mapping[Position, Sequence[Rule]], float | None], Policy]
"""Build the realization under test from a rule table and a budget in seconds."""


@dataclass(frozen=True)
class Case:
    key: str
    """`policy/<invariant key>`."""
    capability: str
    """The AHC capability the invariant serves, as the port file gives it."""
    name: str
    check: Callable[[Make], None]


def _ctx(position: Position, text: str = "hello") -> Context:
    return Context(position=position, identity=Identity(customer_id="C-0001"), text=text)


def _allow(ctx: Context) -> Verdict:
    return ALLOW


def _deny(ctx: Context) -> Verdict:
    return block("deny", "this rule always blocks")


def _deny_too(ctx: Context) -> Verdict:
    return block("deny_too", "this rule always blocks as well")


def _raises(ctx: Context) -> Verdict:
    raise RuntimeError("the detector is down")


def _slow(ctx: Context) -> Verdict:
    time.sleep(1.0)
    return ALLOW


def _valid(verdict: Verdict) -> None:
    jsonschema.validate(decision(verdict), DECISION_SCHEMA)


def _every_decision_has_a_reason(make: Make) -> None:
    for rules, expect_allowed in (
        ({Position.PRE_TOOL: (_allow,)}, True),
        ({Position.PRE_TOOL: ()}, True),
        ({Position.PRE_TOOL: (_deny,)}, False),
        ({Position.PRE_TOOL: (_raises,)}, False),
    ):
        verdict = make(rules, None).evaluate(_ctx(Position.PRE_TOOL))
        assert verdict.allowed is expect_allowed, (rules, verdict)
        _valid(verdict)


def _an_error_takes_the_default(make: Make) -> None:
    policy = make({position: (_raises,) for position in Position}, None)
    assert policy.describe().on_failure == "closed"
    for position in Position:
        verdict = policy.evaluate(_ctx(position))
        assert verdict.blocked, (position, verdict)
        _valid(verdict)


def _a_timeout_takes_the_default(make: Make) -> None:
    policy = make({Position.REPLY: (_slow,)}, 0.05)
    started = time.monotonic()
    verdict = policy.evaluate(_ctx(Position.REPLY))
    assert verdict.blocked, verdict
    assert time.monotonic() - started < 0.5, "the decision waited for the slow rule"
    _valid(verdict)


def _configuration_decides_the_order(make: Make) -> None:
    for order, speaks in (((_deny, _deny_too), "deny"), ((_deny_too, _deny), "deny_too")):
        policy = make({Position.PRE_MODEL: order}, None)
        assert policy.describe().rules[Position.PRE_MODEL] == tuple(r.__name__ for r in order)
        assert policy.evaluate(_ctx(Position.PRE_MODEL)).rule == speaks


def _each_side_runs_its_own_rules(make: Make) -> None:
    policy = make({Position.PRE_MODEL: (_deny,), Position.REPLY: (_allow,)}, None)
    assert policy.evaluate(_ctx(Position.PRE_MODEL)).blocked
    assert policy.evaluate(_ctx(Position.REPLY)).allowed


CASES: tuple[Case, ...] = (
    Case(
        "policy/decision_carries_reason",
        "AHC-0018",
        "every decision, an allow included, carries a reason",
        _every_decision_has_a_reason,
    ),
    Case(
        "policy/failure_takes_declared_default",
        "AHC-0095",
        "a rule that raises takes the declared default, closed",
        _an_error_takes_the_default,
    ),
    Case(
        "policy/failure_takes_declared_default",
        "AHC-0095",
        "a rule past the budget takes the declared default, closed",
        _a_timeout_takes_the_default,
    ),
    Case(
        "policy/order_from_configuration",
        "AHC-0093",
        "configuration's order decides which blocking rule speaks",
        _configuration_decides_the_order,
    ),
    Case(
        "policy/sides_evaluated_separately",
        "AHC-0094",
        "input and output positions run only their own rules",
        _each_side_runs_its_own_rules,
    ),
)


__all__ = ["CASES", "Case", "Make"]
