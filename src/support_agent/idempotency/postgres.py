"""The durable ledger — P6.

The correctness here is the primary key, not the code. `ON CONFLICT DO NOTHING`
means two processes racing on a retry both attempt the insert and only one row
exists afterwards; the loser reads back what the winner wrote. An
application-level check-then-insert would have a window between the two, and the
window is exactly where a double refund lives.
"""

from __future__ import annotations

import json

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from support_agent.contracts import IdempotencyKey, ToolResult


class PostgresLedger:
    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def seen(self, key: IdempotencyKey) -> ToolResult | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT result FROM agent_state.idempotency WHERE key = %s", (key.value,)
                )
            ).fetchone()
        return ToolResult.model_validate(row[0]) if row else None

    async def record(self, key: IdempotencyKey, result: ToolResult) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.idempotency (key, result)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key.value, Jsonb(json.loads(result.model_dump_json()))),
            )
