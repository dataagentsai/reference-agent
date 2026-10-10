"""The `records` port: our own approval and escalation records (A3).

    in-memory   one process, lost on exit (tests, the gates)
    postgres    `agent_state.approvals` and `agent_state.escalations` in the
                agent's database, on a pool of its own

The product is a `RecordStore`. It is built before `approval`, whose waits
write a record in the step that moves their state; a far end reads the same
tables through `ApprovalRecordReader` on a connection of its own. The tables
are the agent's DDL (F-24), run by the `state` port's `migrate` hook, which is
built first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_harness.adapters import Adapter, Setting, Wiring
from agent_harness.contracts.records import RecordStore


@asynccontextmanager
async def _in_memory(wiring: Wiring) -> AsyncIterator[RecordStore]:
    from agent_harness.state.records import InMemoryRecords

    yield InMemoryRecords()


@asynccontextmanager
async def _postgres(wiring: Wiring) -> AsyncIterator[RecordStore]:
    from agent_harness.state.postgres import pool
    from agent_harness.state.records import PostgresRecords

    url = str(wiring.settings["url"])
    async with pool(url, max_size=int(wiring.settings["pool_size"])) as opened:
        yield PostgresRecords(opened)


IN_MEMORY = Adapter("records", "in-memory", _in_memory)
POSTGRES = Adapter(
    "records",
    "postgres",
    _postgres,
    {"url": Setting(required=True, secret=True), "pool_size": Setting(default=2)},
)

__all__ = ["IN_MEMORY", "POSTGRES"]
