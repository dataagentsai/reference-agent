"""The second human.

F-007. Doc 26's taxonomy says a human approver is **an actor and a queue**. We
built the queue — `support_agent.approvals` has expiry, self-approval refusal,
terminal decisions and a stored idempotency key — and never built the actor.

So the entire approval path has only ever been exercised against a reviewer who
is instantaneous, always available, and always says yes. That reviewer does not
exist. And the path guarded by them is the one that moves real money, which
makes the least-modelled participant the guard on the highest-value action.

## What an actor adds that a function call does not

Every existing test decides by calling `decide(...)` inline, at the moment of its
choosing, with `granted=True`. That tests the *queue*. It cannot express the four
things a real reviewer does:

- **takes time** — and the customer is still in the conversation while they do;
- **says no** — and the customer must be told something true about that;
- **walks away** — a pending approval nobody ever answers is the common case in
  any real operations queue, not an edge one;
- **answers too late** — after the grant window has closed, which is a *third*
  outcome distinct from yes and no, and the one most likely to be mishandled.

## Why the decision function is injected

`agenttwin` must not import `support_agent`. The import contract enforces one
direction — the agent cannot see its simulator — and this is the same principle
pointing the other way: a simulator that imported this agent's approval module
would be a simulator for this agent only.

So the actor owns **when** and **what** it decides; the agent's own module owns
**whether that is allowed**. Which is also the honest division of labour: a human
reviewer does not implement the expiry rule, they run into it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from agenttwin.actor import Determinism

Decide = Callable[..., Awaitable[object]]


class Decision(StrEnum):
    GRANT = "grant"
    DENY = "deny"
    SILENCE = "silence"
    """Never answers. Not a failure to configure — a reviewer who went home."""


@dataclass(frozen=True)
class Review:
    """What became of one approval when the reviewer looked at it."""

    approval_id: str
    outcome: str
    """`granted` · `denied` · `waiting` · `refused` — the last when the queue
    rejected the decision, which is how *answered too late* surfaces."""
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.approval_id} {self.outcome}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Approver:
    """A person who reviews pending approvals — or does not.

    Deliberately not a subclass of the conversational actor. It does not speak to
    the agent and the agent never hears from it; it acts on the queue out of
    band, which is exactly how the real thing works.
    """

    store: object
    decide: Decide
    decision: Decision = Decision.GRANT
    delay_s: int = 0
    """How long this reviewer takes. Set beyond the approval's TTL to model the
    reviewer who answers after the window closed."""

    name: str = "ops-7"
    """Who decided, recorded in the audit trail. Set to the customer's own id to
    exercise the confused-deputy refusal."""

    determinism: Determinism = Determinism.SCRIPTED
    reviewed: list[Review] = field(default_factory=list)

    @classmethod
    def grants(cls, store, decide, **kw) -> Approver:
        return cls(store=store, decide=decide, decision=Decision.GRANT, **kw)

    @classmethod
    def denies(cls, store, decide, **kw) -> Approver:
        return cls(store=store, decide=decide, decision=Decision.DENY, **kw)

    @classmethod
    def silent(cls, store, decide, **kw) -> Approver:
        return cls(store=store, decide=decide, decision=Decision.SILENCE, **kw)

    async def review(self, *, at: int | None = None) -> tuple[Review, ...]:
        """Look at the queue once, and decide whatever is due.

        Called between conversational turns, because that is when a real
        reviewer acts: while the customer is still there, and without either of
        them knowing what the other is doing.
        """
        moment = at if at is not None else int(time.time())
        pending = await self.store.pending()  # type: ignore[attr-defined]

        outcomes: list[Review] = []
        for approval in pending:
            outcomes.append(await self._look_at(approval, moment))
        self.reviewed.extend(outcomes)
        return tuple(outcomes)

    async def _look_at(self, approval, moment: int) -> Review:
        if self.decision is Decision.SILENCE:
            return Review(approval.id, "waiting", "nobody picked it up")
        if moment < approval.created_at + self.delay_s:
            return Review(approval.id, "waiting", f"reviewer takes {self.delay_s}s")

        granted = self.decision is Decision.GRANT
        try:
            await self.decide(self.store, approval.id, granted=granted, by=self.name, now=moment)
        except Exception as refused:  # noqa: BLE001
            # The queue's rules belong to the agent, not to its simulator, so the
            # exception type is deliberately not imported. A reviewer who is told
            # "too late" experiences a refusal, not a type.
            return Review(approval.id, "refused", str(refused))

        return Review(approval.id, "granted" if granted else "denied")


__all__ = ["Approver", "Decision", "Review"]
