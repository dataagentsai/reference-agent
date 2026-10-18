"""The record's turns, regrouped as `evalharness.Session`s: what the domain
patterns read (`evalharness.patterns`), one conversation at a time.

The harness owns this mapping because it owns the record (`watch.record`), as
`watch.online.request_of` does for one turn. Each turn becomes, in order: what
the customer said, each tool call with its result, the reply, and how the turn
ended when that ending is a decision a pattern can judge.

A tool's `refused` outcome — the far system answering no — is recorded as not
done (`is_error`): a lookup that was refused is not a lookup that happened, and
a payout that was refused did not pay. `replayed` is a call answered from its
earlier result, so it counts as done.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from evalharness import Session, Step

from agent_harness.contracts.kinds import TerminationReason as Ending
from agent_harness.watch.record import Turn

DECISIONS = frozenset(
    {
        Ending.AWAITING_APPROVAL,
        Ending.AWAITING_HUMAN,
        Ending.REFUSED,
        Ending.DECLINED,
        Ending.CONCERNS_UNANSWERED,
    }
)
"""Endings that are a decision about the conversation, named as the ending is."""
NOT_DONE = frozenset({"error", "refused"})


def session_of(session_id: str, turns: Sequence[Turn]) -> Session:
    """One conversation's turns, oldest first, as a `Session`."""
    steps: list[Step] = []
    ordered = sorted(turns, key=lambda t: t.started)
    for number, turn in enumerate(ordered, start=1):
        if turn.input:
            steps.append(Step("user", text=turn.input, turn=number))
        for i, use in enumerate(turn.tools):
            call_id = f"{turn.run_id}:{i}"
            steps.append(
                Step(
                    "tool_call",
                    name=use.name,
                    arguments=dict(use.arguments or {}),
                    call_id=call_id,
                    turn=number,
                )
            )
            steps.append(
                Step(
                    "tool_result",
                    name=use.name,
                    result=use.result,
                    text=use.result if isinstance(use.result, str) else "",
                    call_id=call_id,
                    is_error=use.outcome in NOT_DONE,
                    turn=number,
                )
            )
        if turn.reply:
            steps.append(Step("assistant", text=turn.reply, turn=number))
        if turn.termination in DECISIONS:
            steps.append(Step("decision", name=turn.termination, turn=number))
    outcome = ordered[-1].termination if ordered else ""
    return Session(id=session_id, steps=tuple(steps), outcome=outcome)


def sessions(turns: Iterable[Turn]) -> list[Session]:
    """Every conversation in `turns`, synthetic ones left out, by first turn."""
    grouped: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        if not turn.synthetic:
            grouped[turn.session_id or turn.run_id].append(turn)
    return sorted(
        (session_of(sid, group) for sid, group in grouped.items()),
        key=lambda s: min(t.started for t in grouped[s.id]),
    )
