"""Where approvals live in one process. `durable = False`, and marked so."""

from __future__ import annotations

import asyncio

from support_agent.contracts import (
    Approval,
)


class InMemoryApprovalStore:
    durable = False

    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}
        self._lock = asyncio.Lock()

    async def put(self, approval: Approval) -> None:
        async with self._lock:
            self._items[approval.id] = approval

    async def get(self, approval_id: str) -> Approval | None:
        async with self._lock:
            return self._items.get(approval_id)

    async def pending(self) -> tuple[Approval, ...]:
        async with self._lock:
            return tuple(a for a in self._items.values() if not a.decided)


__all__ = ["InMemoryApprovalStore"]
