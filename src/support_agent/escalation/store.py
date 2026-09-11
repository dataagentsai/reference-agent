"""Where escalations live in one process. `durable = False`, and marked so."""

from __future__ import annotations

import asyncio

from support_agent.contracts import (
    Escalation,
)


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


__all__ = ["InMemoryEscalationStore"]
