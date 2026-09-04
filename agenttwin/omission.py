"""What should have happened and did not.

The fourth oracle. Doc 31 found the gap by running ten agents through the
boundary method: a clinical triage agent's worst failure is a **missed
escalation**, and every oracle we own reports success on it.

    world diff   nothing changed — correct, nothing was supposed to change
    truth        no false claim — correct, the reply was accurate
    bounds       nothing exceeded — correct, it did almost nothing

An action that should have happened and did not leaves **no evidence**. That is
what makes it different in kind from every other failure we test: the others are
detected by looking at what the run produced, and this one is only detectable by
knowing what the run owed.

## Why the world declares it

`required_when` sits beside `allowed_when` on the same action, in the same
predicate language, because they are the same kind of statement about the same
rule — one says *may*, the other says *must*. Putting the obligation anywhere
else would mean a scenario author had to know a rule the world already knows,
and the moment a second world declares a different rule (the way
`electronics.yaml` moves a return window) the hand-written obligation is wrong
and silent.

## Owed is computed against world₀, checked against effects

A refund on a returned order is owed at the *start* of the run. Doing it sets
the status to `refunded`, so the same query against world₁ returns nothing —
the obligation discharges itself by being met. Asking the final world what is
owed would therefore report success for both the agent that refunded and the
agent that did nothing, which is precisely the blindness this module exists to
remove.

So: derive from the opening state, compare against what the run actually did.

## What it deliberately does not do

An obligation the world cannot express is not expressible here. *"Escalate when
the customer is distressed"* is real, and it is not a field comparison — it needs
a judge, and a judge needs its own validation before anything should trust it.
Stating that limit is better than a grammar that half-supports it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agenttwin.projection import Live
from agenttwin.world import World


@dataclass(frozen=True)
class Obligation:
    """One thing the run owed, and the row that made it owed."""

    action: str
    entity: str
    key: str
    because: str

    def __str__(self) -> str:
        return f"{self.action} on {self.entity} {self.key} ({self.because})"


def owed(world: World, rows: dict[str, dict[str, dict]]) -> tuple[Obligation, ...]:
    """Every action the declared world requires, given these rows.

    `rows` is a snapshot rather than a `Live`, so this can be asked of world₀
    after the run has moved on — which is the only order that produces a useful
    answer.
    """
    out: list[Obligation] = []
    for system in world.systems.values():
        for name, action in system.actions.items():
            if not action.required_when:
                continue
            entity = world.entities.get(action.entity)
            if entity is None:
                continue
            for key, row in rows.get(action.entity, {}).items():
                if all(c.holds(row) for c in action.required_when):
                    out.append(
                        Obligation(
                            action=name,
                            entity=action.entity,
                            key=key,
                            because=_why(action.required_when, row),
                        )
                    )
    return tuple(out)


def _why(conditions, row: dict) -> str:
    """Name the state that created the obligation.

    A verdict saying only *"issue_refund was owed"* sends someone to read the
    world file. One saying *"status is returned"* does not.
    """
    return ", ".join(f"{c.field} is {row.get(c.field)!r}" for c in conditions)


def omitted(live: Live, world_0: dict[str, dict[str, dict]]) -> tuple[Obligation, ...]:
    """Obligations that were owed at the start and never discharged."""
    done = set(live.effects)
    return tuple(o for o in owed(live.world, world_0) if (o.action, o.key) not in done)


def nothing_was_omitted(world_0: dict[str, dict[str, dict]]):
    """A scenario predicate: the run did everything the world required of it.

    Takes the opening snapshot rather than reading it from `Live`, because by
    the time a predicate runs the world has moved and the obligations that
    mattered are the ones that existed before it did.
    """

    def predicate(live: Live, transcript) -> bool:
        return not omitted(live, world_0)

    return predicate


__all__ = ["Obligation", "nothing_was_omitted", "omitted", "owed"]
