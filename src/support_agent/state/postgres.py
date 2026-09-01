"""Durable checkpoints — P6, for real.

`FileCheckpointStore` survives a restart but not two processes. This survives
both, and the difference matters exactly once: when an approval is outstanding,
a human is deciding, and the process that asked has been replaced.

`state` and `idempotency` are the only modules permitted to import psycopg, and
the import contract enforces it. Everything else reaches durability through the
protocols in `contracts`.

The approval store lives here rather than in `approvals` for that reason, and the
contract is what found it: `approvals` is a *policy* module — thresholds, who may
decide, when a grant expires — while the durable substrate belongs to `state`.
Widening the rule to admit a third module would have been the easier fix and the
wrong one.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from support_agent.contracts import Approval, RunId


class PostgresCheckpointStore:
    """One row per run, last write wins.

    `ON CONFLICT DO UPDATE` rather than delete-then-insert: a checkpoint must
    never be briefly absent, because absent is indistinguishable from "this run
    never happened" to anything that resumes.
    """

    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def checkpoint(self, run_id: RunId, state: bytes) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.checkpoints (run_id, state, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (run_id) DO UPDATE
                    SET state = EXCLUDED.state, updated_at = now()
                """,
                (run_id, state),
            )

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT state FROM agent_state.checkpoints WHERE run_id = %s", (run_id,)
                )
            ).fetchone()
        return bytes(row[0]) if row else None


class PostgresApprovalStore:
    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def put(self, approval: Approval) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.approvals
                    (id, action, args, reason, customer_id, idempotency_key,
                     created_at, expires_at, decided, granted, decided_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    decided = EXCLUDED.decided,
                    granted = EXCLUDED.granted,
                    decided_by = EXCLUDED.decided_by
                """,
                (
                    approval.id,
                    approval.action,
                    Jsonb(approval.args),
                    approval.reason,
                    approval.customer_id,
                    approval.idempotency_key,
                    approval.created_at,
                    approval.expires_at,
                    approval.decided,
                    approval.granted,
                    approval.decided_by,
                ),
            )
            # Only the decision fields are updatable on conflict. The action, its
            # arguments and the idempotency key are fixed at request time — a
            # store that let them change would let an approval be granted for one
            # thing and executed as another.

    async def get(self, approval_id: str) -> Approval | None:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM agent_state.approvals WHERE id = %s", (approval_id,)
            )
            row = await cur.fetchone()
        return Approval.model_validate(row) if row else None

    async def pending(self) -> tuple[Approval, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM agent_state.approvals WHERE NOT decided ORDER BY created_at"
            )
            rows = await cur.fetchall()
        return tuple(Approval.model_validate(r) for r in rows)
