"""The `state` port: where a conversation's checkpoints and the request ledger live.

    in-memory                  one process, lost on exit (tests, the gates)
    postgres                   PostgreSQL, the Open Stack's
    azure-postgresql-flexible  the same engine operated by Azure; refuses a URL
                               that does not require TLS

Both stores share one pool. The tables are the agent's to create (the library
ships the adapters, not the DDL: claims-fnol-azure F-24), so an agent hands its
migration in as the hook `migrate`, run before the pool opens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring
from agent_harness.contracts import CheckpointStore, Requests


@dataclass(frozen=True)
class Stores:
    checkpoints: CheckpointStore
    requests: Requests
    """The ledger every delivery and tool call is claimed in (AHC-0053, AHC-0074)."""


@asynccontextmanager
async def _in_memory(wiring: Wiring) -> AsyncIterator[Stores]:
    from agent_harness.requests import InMemoryRequests
    from agent_harness.state import InMemoryCheckpointStore

    yield Stores(InMemoryCheckpointStore(), InMemoryRequests())


@asynccontextmanager
async def _postgres(wiring: Wiring) -> AsyncIterator[Stores]:
    from agent_harness.requests.postgres import PostgresRequests
    from agent_harness.state.postgres import PostgresCheckpointStore, pool

    url = str(wiring.settings["url"])
    migrate = wiring.hooks.get("migrate")
    if migrate is not None:
        await migrate(url)
    async with pool(url, max_size=int(wiring.settings["pool_size"])) as opened:
        yield Stores(PostgresCheckpointStore(opened), PostgresRequests(opened))


@asynccontextmanager
async def _azure_postgres(wiring: Wiring) -> AsyncIterator[Stores]:
    url = str(wiring.settings["url"])
    if not any(f"sslmode={mode}" in url for mode in ("require", "verify-ca", "verify-full")):
        raise OverlayRefused(
            "azure-postgresql-flexible: the URL must require TLS (sslmode=require)"
        )
    async with _postgres(wiring) as stores:
        yield stores


DATABASE = {"url": Setting(required=True, secret=True), "pool_size": Setting(default=4)}

IN_MEMORY = Adapter("state", "in-memory", _in_memory)
POSTGRES = Adapter("state", "postgres", _postgres, DATABASE)
AZURE_POSTGRES = Adapter("state", "azure-postgresql-flexible", _azure_postgres, DATABASE)

__all__ = ["AZURE_POSTGRES", "IN_MEMORY", "POSTGRES", "Stores"]
