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

    **The durable version is a superset, not a swap** (T-003). `settle` runs in a
    `finally`, which does not run when a process is killed outright. Here the
    dictionary dies with the process so nothing is stranded, which is
    self-healing *by accident*; a durable claim outlives its writer, so it needs
    an expiry this one does not. `TemporalDeliveries` has one.
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


class DeliveryLog(Protocol):
    """What `once` needs from a delivery log, and nothing else.

    A protocol rather than the in-memory class, because the durable log T-003
    calls for is a different implementation of the same two calls — and a
    parameter typed `object` let any argument through until the first `claim`.
    """

    async def claim(self, delivery_id: str) -> Delivery: ...

    async def settle(self, delivery_id: str) -> None: ...


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
        await log.settle(delivery_id)


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
