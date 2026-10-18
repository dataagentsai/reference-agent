"""The record's turns, regrouped as `evalharness.Session`s: what the domain
patterns read (`evalharness.patterns`), one conversation at a time.

The harness owns this mapping because it owns the record (`watch.record`), as
`watch.online.request_of` does for one turn. Each turn becomes, in order: what
the customer said, each tool call with its result, the reply, and how the turn
ended when that ending is a decision a pattern can judge. Work done outside a
turn for the conversation (`record.background`: an approval's lookup and the
payout a person approved) goes in where it happened, between turns.

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
from agent_harness.watch.record import ToolUse, Turn, Work

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


def session_of(session_id: str, turns: Sequence[Turn], work: Sequence[Work] = ()) -> Session:
    """One conversation's turns, and the work done for it, oldest first, as a
    `Session`. Work that started while a turn was running (a payout within the
    automatic limit, carried out inside `request_payout`) goes before that
    turn's reply, because it happened before the reply was sent; work between
    turns (a payout a person approved later) goes between them."""
    steps: list[Step] = []
    ordered = sorted(turns, key=lambda t: t.started)
    pending = sorted(work, key=lambda w: w.started)
    for number, turn in enumerate(ordered, start=1):
        while pending and pending[0].started < turn.started:
            w = pending.pop(0)
            steps += _tools(w.trace_id, w.tools, number - 1)
        if turn.input:
            steps.append(Step("user", text=turn.input, turn=number))
        steps += _tools(turn.run_id, turn.tools, number)
        ends = turn.started + turn.duration_s
        while pending and pending[0].started <= ends:
            w = pending.pop(0)
            steps += _tools(w.trace_id, w.tools, number)
        if turn.reply:
            steps.append(Step("assistant", text=turn.reply, turn=number))
        if turn.termination in DECISIONS:
            steps.append(Step("decision", name=turn.termination, turn=number))
    for w in pending:
        steps += _tools(w.trace_id, w.tools, len(ordered))
    outcome = ordered[-1].termination if ordered else ""
    return Session(id=session_id, steps=tuple(steps), outcome=outcome)


def _tools(prefix: str, uses: Sequence[ToolUse], turn: int) -> list[Step]:
    steps = []
    for i, use in enumerate(uses):
        call_id = f"{prefix}:{i}"
        steps.append(
            Step(
                "tool_call",
                name=use.name,
                arguments=dict(use.arguments or {}),
                call_id=call_id,
                turn=turn,
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
                turn=turn,
            )
        )
    return steps


def sessions(turns: Iterable[Turn], work: Iterable[Work] = ()) -> list[Session]:
    """Every conversation in `turns`, synthetic ones left out, by first turn,
    with the background work (`record.background`) that names it merged in."""
    grouped: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        if not turn.synthetic:
            grouped[turn.session_id or turn.run_id].append(turn)
    done_for: dict[str, list[Work]] = defaultdict(list)
    for w in work:
        if w.session_id in grouped:
            done_for[w.session_id].append(w)
    return sorted(
        (session_of(sid, group, done_for[sid]) for sid, group in grouped.items()),
        key=lambda s: min(t.started for t in grouped[s.id]),
    )
