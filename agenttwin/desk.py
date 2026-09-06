"""The third human.

F-007 again, one record along. Doc 26's taxonomy says a human participant is
**an actor and a queue**. For approvals we built the queue first and the actor
months later; for escalations the queue landed in `agent_state.escalations` and
this is the actor that was, once more, not built with it.

The gap is worth naming because it is the same gap twice: a queue is easy to
write and easy to test against yourself, and the party who is supposed to empty
it is the one nobody models. An escalation path exercised only against a desk
that answers instantly and always says *"yes, that needed me"* is a path that has
never met an operations team.

## What a desk does that a function call does not

Every property worth testing here is a property of *time* or of *judgement*, and
neither survives being written as `await resolve(...)` inline at the moment the
test chooses:

- **it takes time** — and the customer is still in the conversation while it does;
- **it disagrees** — *"the agent could have handled this"* is the label that makes
  over-escalation a number rather than an argument, and it is supplied by the
  person closing the ticket, never by us;
- **it never comes** — the common case in any real queue, and the one the whole
  expiry path exists for;
- **it arrives too late** — after the escalation has already lapsed, which is a
  third outcome distinct from answering and from silence.

## Why the closing function is injected

`agenttwin` must not import `support_agent`; the import contract enforces it in
one direction and this is the same principle pointing the other way. A simulator
that imported this agent's escalation module would be a simulator for this agent
only. The desk is handed a `close` callable and knows nothing about what it does.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from agenttwin.actor import Determinism

Close = Callable[..., Awaitable[object]]
"""`(store, escalation_id, *, outcome, by, note, now) -> Escalation`.

Structural, not imported. The desk calls it and reports what happened.
"""


class Answer(StrEnum):
    """What this desk does with whatever it finds in the queue.

    The values are the agent's own outcome vocabulary, deliberately. The desk
    cannot import the agent's enum — the import contract runs one way — so the
    strings are the seam, and matching them is the contract. Building this actor
    is what found that the agent was not validating them on the way in.
    """

    HANDLED = "resolved"
    """Picked it up and dealt with it. The escalation was warranted."""

    AGENT_COULD_HAVE = "agent_could_have"
    """Picked it up and says it should never have been raised.

    The over-escalation label, and the only honest source of one. A run whose
    desk never returns this has not tested the measurement at all — it has
    tested a desk that agrees with us.
    """

    MISROUTED = "misrouted"
    SILENCE = "silence"
    """Nobody comes. Not an error to be handled — the case to be designed for."""


@dataclass(frozen=True)
class Handled:
    """One escalation, as the desk experienced it."""

    escalation_id: str
    outcome: str
    """`resolved` · `agent_could_have` · `misrouted` · `waiting` · `refused` —
    the last when the queue rejected the close, which is how *arrived too late*
    surfaces without the desk having to know the rule that stopped it."""
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.escalation_id} {self.outcome}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Desk:
    """People who pick up escalations — or do not.

    Deliberately not a conversational actor. It never speaks to the agent and the
    agent never hears from it; it works the queue out of band and the customer
    finds out on their next turn. That is exactly how the real thing behaves, and
    modelling it any other way would quietly test a system where the handoff is
    synchronous.
    """

    store: object
    close: Close

    answer: Answer = Answer.HANDLED
    delay_s: int = 0
    """How long before this desk picks anything up. Set beyond the escalation's
    TTL to model the desk that arrives after the customer has already been told
    nobody came."""

    name: str = "desk-3"
    """Who closed it, recorded on the row. Set to the customer's own id to
    exercise the confused-deputy refusal on the escalation path."""

    note: str = ""

    determinism: Determinism = Determinism.SCRIPTED
    handled: list[Handled] = field(default_factory=list)

    @classmethod
    def answers(cls, store, close, **kw) -> Desk:
        return cls(store=store, close=close, answer=Answer.HANDLED, **kw)

    @classmethod
    def says_agent_could_have(cls, store, close, **kw) -> Desk:
        """The desk that disagrees with the rule that raised it."""
        return cls(store=store, close=close, answer=Answer.AGENT_COULD_HAVE, **kw)

    @classmethod
    def never_comes(cls, store, close, **kw) -> Desk:
        return cls(store=store, close=close, answer=Answer.SILENCE, **kw)

    async def review(self, *, at: int | None = None) -> tuple[Handled, ...]:
        """Work the queue once.

        Called between conversational turns, because that is when a real desk
        acts — while the customer is still there, and with neither party aware of
        what the other is doing.
        """
        moment = at if at is not None else int(time.time())
        queued = await self.store.pending()  # type: ignore[attr-defined]

        seen: list[Handled] = []
        for escalation in queued:
            seen.append(await self._pick_up(escalation, moment))
        self.handled.extend(seen)
        return tuple(seen)

    async def _pick_up(self, escalation, moment: int) -> Handled:
        if self.answer is Answer.SILENCE:
            return Handled(escalation.id, "waiting", "nobody picked it up")
        if moment < escalation.created_at + self.delay_s:
            return Handled(escalation.id, "waiting", f"desk takes {self.delay_s}s")

        try:
            await self.close(
                self.store,
                escalation.id,
                outcome=self.answer.value,
                by=self.name,
                note=self.note,
                now=moment,
            )
        except Exception as refused:  # noqa: BLE001
            # The queue's rules belong to the agent, not to its simulator, so the
            # exception type is deliberately not imported. A desk told "that
            # lapsed an hour ago" experiences a refusal, not a type.
            return Handled(escalation.id, "refused", str(refused))

        return Handled(escalation.id, self.answer.value)


__all__ = ["Answer", "Desk", "Handled"]
