"""What the model asked for, turned into calls the harness will make.

Pure, and deliberately holding no state: the trace belongs to the run and the
counting belongs here. Two rules live in this file, and both exist because of
where they are applied rather than what they compute.

**A call's identity is its name and its arguments, with the keys sorted.**
`{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` are the same call, and a detector that
thought otherwise would never fire — which is the failure mode of every
oscillation check somebody wrote against unsorted JSON.

**Circling is judged on that identity, not on the tool.** Reading three different
orders is a trajectory. Reading one order three times is a model that has stopped
making progress and not noticed, and it will spend the whole step budget before
anything else catches it.

**A step's plan is bounded in width, not only the run in length (AHC-0097).**
The step budget counts steps, so a step asking for fifty look-ups — or fifty
writes — spends one and does fifty things. `over_fan_out` names the bound a plan
would pass; the loop refuses such a plan whole, since running its first N would
be a partial result nobody marked (AHC-0025).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from support_agent.config import Budgets
from support_agent.contracts import IdempotencyKey, RunId


@dataclass
class Keys:
    """One run's idempotency keys: minted per call, kept across a retry.

    **A retry keeps the key its first attempt was given.** That is the whole
    difference between a retry and a second execution, and it cannot be read off
    the call — both are the same signature twice. What separates them is whether
    the first one ever came back.

    It matters in exactly one case, and it is the case idempotency exists for: a
    call that landed and whose reply was lost. The world moved and nothing on
    this side was told, so no ledger here can help — a ledger cannot record what
    it was never told. Only the far end can recognise the retry, and only if the
    retry arrives wearing the same name.

    F-039: the key was minted from `len(trace.tool_calls)`, which advances on a
    failed attempt too, so every retry reached the far end as a fresh request and
    was executed again. `IdempotencyKey` had said *a retry keeps run, step and
    iteration* since it was written; nothing implemented it, and the scenario
    that would have noticed did not exist until one was written to look.
    """

    owed: dict[tuple[str, str], IdempotencyKey] = field(default_factory=dict)

    def mint(
        self, call: tuple[str, str], *, run_id: RunId, step: int, iteration: int
    ) -> IdempotencyKey:
        """This call's key — the one it already holds, if it is still owed a reply."""
        key = self.owed.get(call) or IdempotencyKey(run_id=run_id, step=step, iteration=iteration)
        self.owed[call] = key
        return key

    def settled(self, call: tuple[str, str]) -> None:
        """It came back. A later call with this signature is asking for a second
        one, not asking again for this one."""
        self.owed.pop(call, None)


def signature(name: str, arguments: dict[str, object]) -> tuple[str, str]:
    """A call's identity for oscillation purposes: the tool and its arguments."""
    return name, json.dumps(arguments, sort_keys=True, default=str)


def circling(counts: Counter[tuple[str, str]], call: tuple[str, str], threshold: int) -> bool:
    """Whether this exact call has now been made `threshold` times."""
    return counts[call] >= threshold


def over_fan_out(asked: int, so_far: int, budgets: Budgets) -> str | None:
    """`per_step` or `per_turn`: the bound a plan of `asked` calls would pass, after
    `so_far` calls already planned this turn; `None` when it passes neither."""
    if asked > budgets.max_tool_calls_per_step:
        return "per_step"
    if so_far + asked > budgets.max_tool_calls_per_turn:
        return "per_turn"
    return None


__all__ = ["Keys", "circling", "over_fan_out", "signature"]
