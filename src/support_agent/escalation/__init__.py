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

from support_agent import telemetry as tel
from support_agent.contracts import Escalation, EscalationState

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

LAPSED_REPLY = (
    "Nobody has picked up {ticket} yet, so I am back with you in the meantime. "
    "Tell me what you need and I will do what I can."
)
"""The honest version of a bad outcome. It does not pretend the escalation
succeeded, and it does not leave the customer with nothing."""


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
    "DEFAULT_TTL_S",
    "LAPSED_REPLY",
    "RAISED_REPLY",
    "WAITING_REPLY",
    "InMemoryEscalationStore",
    "lapse",
    "new_escalation_id",
    "raise_for",
]
