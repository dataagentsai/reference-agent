"""The claim itself, in one process — and what it means to claim a delivery.

The reasoning is in the package docstring next door; this is the types every
delivery log speaks and the realisation that answers for one process.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from support_agent.contracts.failures import AgentFailure, Fault


class TriggerRefused(AgentFailure):
    """This delivery must not start a run."""

    fault = Fault.REFUSED


class DuplicateDelivery(TriggerRefused):
    """Already handled, and `outcome` is what the first attempt answered.

    A redelivery is normal and is not an error, so the caller is handed the
    answer rather than told that one exists. `outcome` is `None` only where the
    first attempt recorded nothing — a claim taken before this carried one, or
    a caller that had nothing to record — and the caller then has to say so.
    """

    def __init__(self, message: str, *, outcome: dict[str, object] | None = None) -> None:
        self.outcome = outcome
        super().__init__(message)


class OverlappingRun(TriggerRefused):
    """Still running. Distinct from a duplicate on purpose: a redelivery is
    routine, and two concurrent turns for one conversation is a race worth
    looking at."""


class State(StrEnum):
    IN_FLIGHT = "in_flight"
    SETTLED = "settled"


@dataclass
class Delivery:
    """A held claim. Not frozen, because `outcome` is written by the body that
    holds it: the claim is taken before the work and settled after it, and what
    the work answered is the only thing worth keeping."""

    id: str
    state: State
    outcome: dict[str, object] | None = None


class InMemoryDeliveryLog:
    """Deliberately in memory, and deliberately marked as such.

    `durable = False` is the honest statement: this survives neither a restart
    nor a second process, so it answers the obligation for one instance and not
    for a deployment.

    **The durable version is a superset, not a swap** (T-003). `settle` runs in a
    `finally`, which does not run when a process is killed outright. Here the
    dictionary dies with the process so nothing is stranded, which is
    self-healing *by accident*; a durable claim outlives its writer, so it needs
    an expiry this one does not. `TemporalDeliveries` has one.
    """

    durable = False

    def __init__(self) -> None:
        self._seen: dict[str, tuple[State, dict[str, object] | None]] = {}
        self._lock = asyncio.Lock()

    async def claim(self, delivery_id: str) -> Delivery:
        async with self._lock:
            existing = self._seen.get(delivery_id)
            if existing is not None and existing[0] is State.SETTLED:
                raise DuplicateDelivery(
                    f"delivery {delivery_id!r} was already handled", outcome=existing[1]
                )
            if existing is not None and existing[0] is State.IN_FLIGHT:
                raise OverlappingRun(f"delivery {delivery_id!r} is already running")
            self._seen[delivery_id] = (State.IN_FLIGHT, None)
            return Delivery(id=delivery_id, state=State.IN_FLIGHT)

    async def settle(self, delivery_id: str, outcome: dict[str, object] | None = None) -> None:
        async with self._lock:
            self._seen[delivery_id] = (State.SETTLED, outcome)

    async def state_of(self, delivery_id: str) -> State | None:
        async with self._lock:
            entry = self._seen.get(delivery_id)
            return None if entry is None else entry[0]


class DeliveryLog(Protocol):
    """What `once` needs from a delivery log, and nothing else.

    A protocol rather than the in-memory class, because the durable log T-003
    calls for is a different implementation of the same two calls — and a
    parameter typed `object` let any argument through until the first `claim`.
    """

    async def claim(self, delivery_id: str) -> Delivery: ...

    async def settle(self, delivery_id: str, outcome: dict[str, object] | None = None) -> None: ...


@asynccontextmanager
async def once(log: DeliveryLog, delivery_id: str | None) -> AsyncIterator[Delivery | None]:
    """Run the body at most once for this delivery.

    A `None` id means the caller did not identify the delivery, and the run
    proceeds unguarded — the honest behaviour, because inventing an id here
    would produce a guard that can never fire and a green report to go with it.
    Callers that care supply one.
    """
    if delivery_id is None:
        yield None
        return

    claim = await log.claim(delivery_id)
    try:
        yield claim
    finally:
        # Settled even on failure: a delivery that was tried and failed has
        # still been delivered, and re-running it on redelivery would repeat
        # whatever effects it managed before failing.
        #
        # And settled *with what it answered*, which the body wrote onto the
        # claim. Without it a redelivery learns only that something happened,
        # never what — so a customer whose reply was lost, and whose order was
        # in fact cancelled, is told "already handled" and nothing else. The
        # answer existed the whole time and nothing could reach it.
        await log.settle(delivery_id, claim.outcome)


__all__ = [
    "Delivery",
    "DeliveryLog",
    "DuplicateDelivery",
    "InMemoryDeliveryLog",
    "OverlappingRun",
    "State",
    "TriggerRefused",
    "once",
]
