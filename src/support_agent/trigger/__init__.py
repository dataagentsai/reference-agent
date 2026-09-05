"""What starts a turn, and whether it should have.

L15 · L10 · P1. AAC-0076 — *triggers fire once and only once*, and **AHC-0053**,
*a trigger produces exactly one run*.

The layers were wrong here until 2026-09-05: this module claimed L14, which is
human-in-the-loop, and a trigger is plainly not a person. The catalogue already
had the right home — L15 because a trigger is configuration, L10 because
duplicate firing is a failure mode — and nothing checked our claim against it.
A mislabelled layer is not cosmetic: it is how a capability ends up counted in
the wrong row of a coverage report.

The obligation is about the edge, not the agent: **an agent that runs twice takes
every action twice**, and it does so correctly each time. Nothing inside the run
is wrong, which is why nothing inside the run can catch it.

## Why idempotency does not already cover this

The ledger keys on `run + step + iteration`. A duplicate delivery produces a
whole second run with its own run id, so the two never share a key space and the
ledger cannot see across them. R-006 found the same thing from the inside — a
customer who asks three times is three runs — and this is the outside half of
it. **The defence sat one layer below where the duplicate happens.**

## Three failures, and this module answers two

*Fires twice.* A webhook redelivered because the acknowledgement was slow, a
customer clicking Send twice, a queue with at-least-once semantics — which is
every queue worth using. Answered here: a settled delivery is refused.

*Fires again mid-run.* Two turns for one conversation in flight at once, each
reading state before the other has written. Answered here: an in-flight delivery
is refused, and told apart from a duplicate because the operator response
differs — one is a retry to swallow, the other is a race to investigate.

*Never fires.* Not answerable here, and pretending otherwise would be the worst
option: **you cannot detect an absence from inside the thing that is absent.**
It needs a clock the monitoring side owns (AHC-0055), and in a test it is
`agenttwin.omission` — a turn that never arrives means a required effect never
happens, and the omission oracle sees exactly that. One idea, two positions.

## Claim, then settle

A delivery is claimed before the run and settled after it. The gap between the
two is what makes an overlapping run visible at all; a design that only recorded
completed deliveries would let two concurrent copies both pass the check.

Settling happens even when the run fails. A delivery that was tried and failed
has still been delivered, and re-running it on redelivery would repeat whatever
side effects it managed before failing.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum


class TriggerRefused(Exception):
    """This delivery must not start a run."""


class DuplicateDelivery(TriggerRefused):
    """Already handled. The caller should treat the first outcome as the outcome
    rather than retrying — a redelivery is normal and is not an error."""


class OverlappingRun(TriggerRefused):
    """Still running. Distinct from a duplicate on purpose: a redelivery is
    routine, and two concurrent turns for one conversation is a race worth
    looking at."""


class State(StrEnum):
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"


@dataclass(frozen=True)
class Delivery:
    id: str
    state: State


class InMemoryDeliveryLog:
    """Deliberately in memory, and deliberately marked as such.

    `durable = False` is the honest statement: this survives neither a restart
    nor a second process, so it answers the obligation for one instance and not
    for a deployment.

    **The durable version is a superset, not a swap** — an earlier draft of this
    docstring said otherwise and was wrong (T-003). `settle` runs in a `finally`,
    which does not run when a process is killed outright. Here the dictionary
    dies with the process so nothing is stranded, which is self-healing *by
    accident*. A durable `in_flight` row outlives its writer and nothing would
    ever settle it, so one crash would wedge that message permanently. A durable
    claim therefore needs an expiry this one does not.
    """

    durable = False

    def __init__(self) -> None:
        self._seen: dict[str, State] = {}
        self._lock = asyncio.Lock()

    async def claim(self, delivery_id: str) -> Delivery:
        async with self._lock:
            existing = self._seen.get(delivery_id)
            if existing is State.SETTLED:
                raise DuplicateDelivery(f"delivery {delivery_id!r} was already handled")
            if existing is State.IN_FLIGHT:
                raise OverlappingRun(f"delivery {delivery_id!r} is already running")
            self._seen[delivery_id] = State.IN_FLIGHT
            return Delivery(id=delivery_id, state=State.IN_FLIGHT)

    async def settle(self, delivery_id: str) -> None:
        async with self._lock:
            self._seen[delivery_id] = State.SETTLED

    async def state_of(self, delivery_id: str) -> State | None:
        async with self._lock:
            return self._seen.get(delivery_id)


@asynccontextmanager
async def once(log: object, delivery_id: str | None):
    """Run the body at most once for this delivery.

    A `None` id means the caller did not identify the delivery, and the run
    proceeds unguarded — the honest behaviour, because inventing an id here
    would produce a guard that can never fire and a green report to go with it.
    Callers that care supply one.
    """
    if delivery_id is None:
        yield None
        return

    claim = await log.claim(delivery_id)  # type: ignore[attr-defined]
    try:
        yield claim
    finally:
        # Settled even on failure: a delivery that was tried and failed has
        # still been delivered, and re-running it on redelivery would repeat
        # whatever effects it managed before failing.
        await log.settle(delivery_id)  # type: ignore[attr-defined]


__all__ = [
    "Delivery",
    "DuplicateDelivery",
    "InMemoryDeliveryLog",
    "OverlappingRun",
    "State",
    "TriggerRefused",
    "once",
]
