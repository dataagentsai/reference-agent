"""Handing a conversation to a person.

L14 · P6 and P8, and the sibling of `approvals`. The split between them is the
one the import contract already forced once: this module is *policy* — what a
raise means, how long it stays open, what the customer is told — while the
durable substrate belongs to `state`. `PostgresEscalationStore` therefore lives
in `state.postgres`, exactly where `PostgresApprovalStore` does.

## What this fixes

Before it, `Escalate` produced a sentence and nothing else. The agent told a
customer *"let me pass you to a colleague"* — a promise no tool had confirmed,
which the system prompt three modules away explicitly forbids — and then, on the
next turn, answered them itself, because nothing recorded that a handoff had
happened. `ticket_id` had been declared on `Escalated` since the beginning and
was never once set.

## Why an escalation expires

`approvals` has an expiry because a decision can arrive too late to act on. This
has one because **nobody may come at all**, and a conversation held open against
a colleague who never appeared is worse than one that lapses and says so. There
is no reviewer surface yet — that is step 4 — so without expiry the first version
of this module would silently brick every escalated conversation.

The lapse is therefore a designed path, not a failure: the conversation returns
to the agent, the customer is told the truth, and the record survives as
evidence that nobody answered. That last part is the point. *Failing to act is
the failure mode with no evidence*, and this is the one place we can leave some.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from support_agent import telemetry as tel
from support_agent.contracts import Escalation, EscalationOutcome, EscalationState

DEFAULT_TTL_S = 30 * 60
"""How long a queued escalation waits before it lapses.

Thirty minutes is a placeholder with a real shape: it should come from the
desk's actual answer time, and it is deliberately short enough that the lapse
path is exercised rather than theoretical.
"""

RAISED_REPLY = (
    "Let me pass you to a colleague. Your reference is {ticket}, and they will pick this up here."
)
"""Says only what is now true. The record exists and carries this id; nothing is
claimed about when a person will arrive, because nothing here knows."""

WAITING_REPLY = (
    "That is still with a colleague — your reference is {ticket}. I have not forgotten about it."
)

QUEUED_REPLY = (
    "Let me pass you to a colleague — your reference is {ticket}, and the wait is about {wait}."
)
"""Says a number, and only a number it can defend.

The estimate comes from queue depth divided by measured throughput. When there
is nothing to divide by — a desk whose rate we do not know — the agent falls
back to `RAISED_REPLY`, which promises a reference and no time at all. An
invented "just a few minutes" is the whole class of thing the system prompt
forbids."""

CLOSED_REPLY = (
    "The team is not available right now. I have logged this as {ticket} and "
    "they will pick it up when they are back."
)
"""Nobody is there. Saying so beats a reference number that implies somebody
is."""

LAPSED_REPLY = (
    "Nobody has picked up {ticket} yet, so I am back with you in the meantime. "
    "Tell me what you need and I will do what I can."
)
"""The honest version of a bad outcome. It does not pretend the escalation
succeeded, and it does not leave the customer with nothing."""


@dataclass(frozen=True)
class Capacity:
    """Whether anyone is actually there, and how long the queue really is.

    The current design's worst habit was promising a colleague with nothing
    behind it. This is the smallest honest correction: before speaking, ask how
    many are waiting and whether the desk is open, and say only what those two
    numbers support.

    `per_hour` is deliberately optional. A desk whose throughput nobody has
    measured produces no estimate rather than a plausible one — the agent then
    promises a reference and no time, which is true.
    """

    open: bool = True
    per_hour: float | None = None
    """Escalations this desk closes per hour, measured. Not a target."""

    def estimate_s(self, depth: int) -> int | None:
        if not self.per_hour or self.per_hour <= 0:
            return None
        return int((depth / self.per_hour) * 3600)


def humanise(seconds: int) -> str:
    """A wait a person can act on. Rounded, because false precision in a promise
    reads as a guarantee."""
    if seconds < 90:
        return "a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = round(minutes / 60)
    return "an hour" if hours == 1 else f"{hours} hours"


async def sweep(store: object, *, now: int | None = None) -> tuple[Escalation, ...]:
    """Lapse everything nobody came for.

    The lapse path was lazy: it only ran when the customer sent another turn. So
    an escalation on a conversation somebody abandoned never expired, and sat in
    the desk's queue as permanent phantom work — the queue depth that the wait
    estimate above divides by would drift upward forever, and every promise made
    from it would get worse.

    Deliberately a plain function rather than a background thread. The caller
    decides the cadence: a periodic task in the server, one call in a test, a
    cron job in a deployment. A sweeper that owns its own scheduling is one
    nobody can drive from a scenario.
    """
    moment = int(time.time()) if now is None else now
    lapsed: list[Escalation] = []
    for escalation in await store.pending():  # type: ignore[attr-defined]
        if escalation.lapsed(moment):
            lapsed.append(await lapse(store, escalation, now=moment))
    return tuple(lapsed)


def new_escalation_id() -> str:
    """Short and readable, because a customer says it out loud.

    `E-` plus eight hex characters rather than a bare uuid: the id appears in the
    reply, and a reference a person cannot read back over the phone is one they
    will not use.
    """
    return f"E-{uuid.uuid4().hex[:8].upper()}"


class InMemoryEscalationStore:
    """Process-local, and marked as such.

    `durable = False` is the same honest statement `InMemoryDeliveryLog` makes:
    this answers the obligation for one process and not for a deployment. An
    escalation is exactly the record that must outlive the process that wrote it,
    so this is for tests and the demo server; `PostgresEscalationStore` is the
    one that means it.
    """

    durable = False

    def __init__(self) -> None:
        self._items: dict[str, Escalation] = {}
        self._lock = asyncio.Lock()

    async def put(self, escalation: Escalation) -> None:
        async with self._lock:
            self._items[escalation.id] = escalation

    async def get(self, escalation_id: str) -> Escalation | None:
        async with self._lock:
            return self._items.get(escalation_id)

    async def open_for(self, conversation_id: str) -> Escalation | None:
        async with self._lock:
            found = [
                e for e in self._items.values() if e.conversation_id == conversation_id and e.open
            ]
        # Newest wins. Two open escalations on one conversation should not
        # happen — the entrypoint short-circuits before it can raise a second —
        # but choosing deterministically beats returning whichever the dict
        # happened to yield first.
        return max(found, key=lambda e: e.created_at) if found else None

    async def pending(self) -> tuple[Escalation, ...]:
        async with self._lock:
            return tuple(
                sorted((e for e in self._items.values() if e.open), key=lambda e: e.created_at)
            )


async def raise_for(
    store: object,
    *,
    conversation_id: str,
    run_id: str,
    customer_id: str,
    reason: str,
    rule_id: str,
    rules_version: str,
    tier: int = 1,
    ttl_s: int = DEFAULT_TTL_S,
    now: int | None = None,
) -> Escalation:
    """Write the record, then let the caller speak.

    Deliberately in this order. Everything that has gone wrong with this path
    came from telling the customer first and recording second — which is to say,
    never recording at all.
    """
    moment = int(time.time()) if now is None else now
    escalation = Escalation(
        id=new_escalation_id(),
        conversation_id=conversation_id,
        run_id=run_id,
        customer_id=customer_id,
        tier=tier,
        rule_id=rule_id,
        rules_version=rules_version,
        reason=reason,
        state=EscalationState.QUEUED,
        created_at=moment,
        expires_at=moment + ttl_s,
    )
    with tel.span(
        "agent.escalation.raise",
        **{
            tel.ESCALATION_ID: escalation.id,
            tel.ESCALATION_TIER: tier,
            tel.ESCALATION_RULE: rule_id,
            "agent.escalation.rules_version": rules_version,
        },
    ):
        await store.put(escalation)  # type: ignore[attr-defined]
    return escalation


class EscalationError(Exception):
    """This escalation cannot be closed that way."""


async def resolve(
    store: object,
    escalation_id: str,
    *,
    outcome: EscalationOutcome | str,
    by: str,
    note: str = "",
    now: int | None = None,
) -> Escalation:
    """A person closes it, and says what it was.

    Three refusals, fail-closed, and each is `approvals.decide`'s reasoning in
    the shape this record has:

    **Nobody closes their own escalation.** `by` may not be the customer — the
    confused deputy of the human path, and the reason the outcome label is worth
    anything. A customer who could mark their own case resolved would make the
    over-escalation rate a number the measured party writes.

    **Closing is terminal.** Re-resolving is refused rather than overwritten. An
    outcome that can be rewritten is an outcome that can be rewritten *after*
    someone reads the dashboard.

    **A lapsed escalation cannot be resolved.** Nobody came; recording that
    somebody did, an hour later, would erase precisely the evidence the expiry
    exists to leave.

    `outcome` is required rather than defaulted. A reviewer who closes without
    saying whether the agent could have handled it has given us nothing, and
    letting that pass silently is how the false-positive rate stays unmeasurable
    forever.
    """
    # Coerced at the boundary, not trusted from it. Every caller of this is
    # across a wire or a seam — an HTTP reviewer surface, a simulated desk — and
    # none of them hands over a Python enum. `model_copy(update=...)` does not
    # validate, so an unchecked string would land in the row and surface later as
    # an outcome no dashboard has a bucket for.
    try:
        label = EscalationOutcome(outcome)
    except ValueError:
        raise EscalationError(
            f"{outcome!r} is not an outcome; expected one of {[o.value for o in EscalationOutcome]}"
        ) from None

    moment = int(time.time()) if now is None else now
    escalation = await store.get(escalation_id)  # type: ignore[attr-defined]
    if escalation is None:
        raise EscalationError(f"no escalation {escalation_id!r}")
    if not escalation.open:
        raise EscalationError(f"escalation {escalation_id!r} is already {escalation.state.value}")
    if escalation.lapsed(moment):
        raise EscalationError(f"escalation {escalation_id!r} lapsed before anyone came")
    if by == escalation.customer_id:
        raise EscalationError("an escalation cannot be closed by the customer it belongs to")

    closed = escalation.model_copy(
        update={
            "state": EscalationState.RESOLVED,
            "resolved_at": moment,
            "outcome": label,
            "outcome_by": by,
            "outcome_note": note or None,
        }
    )
    with tel.span(
        "agent.escalation.resolve",
        **{
            tel.ESCALATION_ID: closed.id,
            tel.ESCALATION_RULE: closed.rule_id,
            "agent.escalation.outcome": label.value,
            "agent.escalation.waited_s": moment - closed.created_at,
        },
    ):
        await store.put(closed)  # type: ignore[attr-defined]
    return closed


async def lapse(store: object, escalation: Escalation, *, now: int | None = None) -> Escalation:
    """Nobody came. Close it as expired and hand the conversation back."""
    moment = int(time.time()) if now is None else now
    expired = escalation.model_copy(
        update={"state": EscalationState.EXPIRED, "resolved_at": moment}
    )
    with tel.span(
        "agent.escalation.lapse",
        **{
            tel.ESCALATION_ID: expired.id,
            tel.ESCALATION_RULE: expired.rule_id,
            "agent.escalation.waited_s": moment - expired.created_at,
        },
    ):
        await store.put(expired)  # type: ignore[attr-defined]
    return expired


__all__ = [
    "CLOSED_REPLY",
    "DEFAULT_TTL_S",
    "QUEUED_REPLY",
    "Capacity",
    "EscalationError",
    "LAPSED_REPLY",
    "RAISED_REPLY",
    "WAITING_REPLY",
    "InMemoryEscalationStore",
    "humanise",
    "lapse",
    "new_escalation_id",
    "raise_for",
    "resolve",
    "sweep",
]
