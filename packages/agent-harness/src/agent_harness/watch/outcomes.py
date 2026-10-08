"""What happened after a turn, attached to the turn it judges (AHC-0112, AAC-0115).

A rule reads what the agent said. An outcome says whether it was right, and
arrives later, from the customer's own behaviour:

- `returned`, inferred: the customer opened a new conversation within a day
  of one that ended *completed* — the answer did not hold.
- `asked_for_person`, inferred: the next message in the same conversation
  asked for a person — the answer did not satisfy.
- `feedback_up` / `feedback_down`, stated: the customer said so.

Inferred outcomes are labelled as inferred, and the inference is versioned like
a rule (`OUTCOME_VERSION`), because it is one: a customer who comes back to buy
something else is counted as `returned` too, and the rate is read with that in
mind. A refund reversed would belong here and has no source yet: the store has
no reversal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from agent_harness.contracts.reading import vocabulary
from agent_harness.watch.record import Feedback, Turn

OUTCOME_VERSION = "3"
RETURN_WINDOW_S = 24 * 3600
FOLLOW_UP_S = 30 * 60
"""A new conversation sooner than this is a follow-up, not a return. Version 1
had no floor, and the first live run counted a customer asking four questions
in a minute as four failed answers."""


@dataclass(frozen=True)
class Outcome:
    kind: str
    source: Literal["inferred", "stated"]
    trace_id: str
    session_id: str
    user_id: str
    detail: str


def returned(new: Iterable[Turn], history: Iterable[Turn]) -> list[Outcome]:
    """A new conversation within a day of a completed one, by the same customer."""
    known = sorted({t.trace_id: t for t in (*history, *new)}.values(), key=lambda t: t.started)
    out: dict[str, Outcome] = {}
    for turn in new:
        if turn.synthetic or _earlier_in_session(turn, known) or _in_a_burst(turn, known):
            continue
        before = [
            p
            for p in known
            if p.user_id == turn.user_id
            and p.session_id != turn.session_id
            and turn.started - RETURN_WINDOW_S <= p.started <= turn.started - FOLLOW_UP_S
        ]
        if not before or before[-1].result != "completed":
            continue
        last = before[-1]
        detail = f"new conversation {(turn.started - last.started) / 3600:.1f}h later"
        same = _orders(last) & _orders(turn)
        if same:
            detail += f", about {', '.join(sorted(same))}"
        out.setdefault(
            last.trace_id,
            Outcome("returned", "inferred", last.trace_id, last.session_id, last.user_id, detail),
        )
    return list(out.values())


def asked_for_person(turns: Iterable[Turn]) -> list[Outcome]:
    """A completed answer followed, in the same conversation, by a request for a person."""
    by_session: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        by_session[turn.session_id].append(turn)
    out = []
    for session in by_session.values():
        ordered = sorted(session, key=lambda t: t.started)
        for previous, following in zip(ordered, ordered[1:], strict=False):
            if previous.result == "completed" and following.route == "escalate":
                out.append(
                    Outcome(
                        "asked_for_person",
                        "inferred",
                        previous.trace_id,
                        previous.session_id,
                        previous.user_id,
                        "the next message asked for a person",
                    )
                )
    return [o for o in out if not _synthetic(o, turns)]


def stated(feedback: Iterable[Feedback], turns: Iterable[Turn]) -> list[Outcome]:
    """The customer's own verdict, on the last turn before it in that conversation."""
    ordered = sorted(turns, key=lambda t: t.started)
    out = []
    for given in feedback:
        before = [t for t in ordered if t.session_id == given.session_id and t.started <= given.at]
        if before:
            last = before[-1]
            out.append(
                Outcome(
                    f"feedback_{given.value}",
                    "stated",
                    last.trace_id,
                    last.session_id,
                    last.user_id,
                    f"the customer said {given.value}",
                )
            )
    return out


def _in_a_burst(turn: Turn, known: list[Turn]) -> bool:
    """Another of this customer's conversations began just before: this one is
    part of the same return, not a second one (found live: a burst of eight
    new conversations counted one return eight times)."""
    return any(
        t.user_id == turn.user_id
        and t.session_id != turn.session_id
        and turn.started - FOLLOW_UP_S < t.started < turn.started
        for t in known
    )


def _earlier_in_session(turn: Turn, known: list[Turn]) -> bool:
    return any(t.session_id == turn.session_id and t.started < turn.started for t in known)


def _orders(turn: Turn) -> set[str]:
    return vocabulary().identifiers(turn.input or "")


def _synthetic(outcome: Outcome, turns: Iterable[Turn]) -> bool:
    return any(t.trace_id == outcome.trace_id and t.synthetic for t in turns)


__all__ = [
    "OUTCOME_VERSION",
    "FOLLOW_UP_S",
    "RETURN_WINDOW_S",
    "Outcome",
    "asked_for_person",
    "returned",
    "stated",
]
