"""The `policy` port, named as AHC names it: `evaluate` and `describe` (T-048).

`enforce` is how our own loop runs the rules. This is the same thing behind the
port's two operations, so a loop we do not own (a LangGraph node, a Claude
Agent SDK hook, a Pydantic AI capability) can call the rules through one
interface, and every such adapter is held to the same conformance cases
(`policy.conformance`).

    evaluate   one position's rules over one context: a decision with a reason
    describe   which rules run where, in what order, what a failure becomes,
               and the time budget, without running any traffic

Every decision carries a reason identifier, an allow included
(`policy/decision_carries_reason`): an allow that says nothing cannot be told
from a position nobody configured. What crosses the port is
`DECISION_SCHEMA`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_harness.policy import Rule, default_rules, enforce
from agent_harness.policy.verdicts import Context, Position, Verdict

ALLOWED = "policy.allowed"
"""The reason identifier of an allow: every rule at the position ran and none blocked."""

ON_FAILURE: Literal["closed"] = "closed"
"""What a rule that errors or runs past the budget becomes. Declared, and closed
(`policy/failure_takes_declared_default`); no realization here offers open."""


@dataclass(frozen=True)
class Description:
    """What `describe` returns: the composition, inspectable without traffic."""

    rules: Mapping[Position, tuple[str, ...]]
    """Rule names per position, in the order they run, which is configuration's
    order and never the implementation's (`policy/order_from_configuration`)."""
    on_failure: Literal["closed"]
    budget_s: float | None


class Policy(Protocol):
    """The port. A realization owns how rules run; the rules are the agent's."""

    def evaluate(self, ctx: Context) -> Verdict: ...

    def describe(self) -> Description: ...


class RulePolicy:
    """The reference realization: the agent's rule table through `enforce`.

    With no table it reads the registered defaults (`use_default_rules`) at each
    call, as `enforce` does, so a policy built before the agent registers its
    rules still sees them.
    """

    def __init__(
        self,
        rules: Mapping[Position, Sequence[Rule]] | None = None,
        *,
        budget_s: float | None = None,
    ) -> None:
        self._rules = rules
        self.budget_s = budget_s

    def _table(self) -> Mapping[Position, Sequence[Rule]]:
        return default_rules() if self._rules is None else self._rules

    def evaluate(self, ctx: Context) -> Verdict:
        verdict = enforce(ctx, tuple(self._table().get(ctx.position, ())), budget_s=self.budget_s)
        if verdict.blocked:
            return verdict
        return Verdict(
            allowed=True, rule=ALLOWED, reason=f"no rule blocked at {ctx.position.value}"
        )

    def describe(self) -> Description:
        return Description(
            rules={
                position: tuple(getattr(rule, "__name__", repr(rule)) for rule in rules)
                for position, rules in self._table().items()
            },
            on_failure=ON_FAILURE,
            budget_s=self.budget_s,
        )


def decision(verdict: Verdict) -> dict[str, object]:
    """A decision as it crosses the port and enters a record."""
    return {"allowed": verdict.allowed, "rule": verdict.rule, "reason": verdict.reason}


DECISION_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "agent-harness/policy/decision",
    "title": "A policy decision",
    "type": "object",
    "required": ["allowed", "rule", "reason"],
    "additionalProperties": False,
    "properties": {
        "allowed": {"type": "boolean"},
        "rule": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
}


__all__ = [
    "ALLOWED",
    "DECISION_SCHEMA",
    "ON_FAILURE",
    "Description",
    "Policy",
    "RulePolicy",
    "decision",
]
