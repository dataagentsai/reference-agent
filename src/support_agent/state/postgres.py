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

from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from support_agent.contracts import ConversationId, RunId, StoredSession
from support_agent.contracts.failures import AgentFailure, Fault


class PostgresCheckpointStore:
    """One row per run, last write wins.

    `ON CONFLICT DO UPDATE` rather than delete-then-insert: a checkpoint must
    never be briefly absent, because absent is indistinguishable from "this run
    never happened" to anything that resumes.
    """

    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def checkpoint(
        self,
        run_id: RunId,
        state: bytes,
        *,
        conversation_id: ConversationId,
        customer_id: str = "",
    ) -> None:
        async with self._pool.connection() as conn:
            # One statement, both indexes — F-006. The conversation id goes in
            # the same row rather than a second table, so the two can never
            # disagree and no transaction is needed to keep them together. The
            # customer id rides along for the same reason (F-056): a second
            # table mapping runs to people is a table that can fall behind.
            await conn.execute(
                """
                INSERT INTO agent_state.checkpoints
                    (run_id, conversation_id, customer_id, state, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE
                    SET state = EXCLUDED.state,
                        conversation_id = EXCLUDED.conversation_id,
                        customer_id = EXCLUDED.customer_id,
                        updated_at = now()
                """,
                (run_id, conversation_id, customer_id or None, state),
            )

    async def forget(self, customer_id: str) -> tuple[RunId, ...]:
        """One statement — F-056.

        `DELETE ... RETURNING` rather than select-then-delete, because between
        those two the same customer can start another turn, and it would be
        returned as erased while its row is still there.

        An empty id matches nothing. It is what an unattributed row carries,
        and `customer_id = NULL` never equals anything anyway — but the guard
        is here rather than left to SQL's three-valued logic, because the cost
        of being wrong about that once is every conversation in the table.
        """
        if not customer_id:
            return ()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "DELETE FROM agent_state.checkpoints WHERE customer_id = %s RETURNING run_id",
                    (customer_id,),
                )
            ).fetchall()
        return tuple(RunId(row[0]) for row in rows)

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        """The newest turn of this conversation.

        `ORDER BY updated_at DESC LIMIT 1` because a conversation has many runs
        and only the last one is state worth resuming from. The index on
        (conversation_id, updated_at) is what stops this being a table scan on
        a busy day.
        """
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT state FROM agent_state.checkpoints
                    WHERE conversation_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                )
            ).fetchone()
        return None if row is None else bytes(row[0])

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT state FROM agent_state.checkpoints WHERE run_id = %s", (run_id,)
                )
            ).fetchone()
        return bytes(row[0]) if row else None


class UnreadableSession(AgentFailure):
    """A stored session this key cannot decrypt: rotated keys, or a row written by
    something else. Treated as no session, never as a partial one."""

    fault = Fault.MISCONFIGURED


class PostgresSessionStore:
    """Stored sessions, with the refresh token encrypted at rest (T-026).

    Fernet, with the key held by the process and never by the database, so a
    dump of `agent_state` yields ciphertext. The subject is not encrypted: it is
    the lookup key, and it is an opaque login id, not the customer.
    """

    durable = True

    def __init__(self, pool: AsyncConnectionPool, *, key: bytes) -> None:
        self._pool = pool
        self._cipher = Fernet(key)

    async def put(self, session: StoredSession) -> None:
        sealed = self._cipher.encrypt(session.refresh_token.encode())
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.sessions (subject, refresh_token, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (subject) DO UPDATE
                    SET refresh_token = EXCLUDED.refresh_token,
                        updated_at = EXCLUDED.updated_at
                """,
                (session.subject, sealed, session.updated_at),
            )

    async def get(self, subject: str) -> StoredSession | None:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM agent_state.sessions WHERE subject = %s", (subject,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            token = self._cipher.decrypt(bytes(row["refresh_token"])).decode()
        except InvalidToken as exc:
            raise UnreadableSession(f"session for {subject!r} does not decrypt") from exc
        return StoredSession(
            subject=row["subject"], refresh_token=token, updated_at=row["updated_at"]
        )

    async def delete(self, subject: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM agent_state.sessions WHERE subject = %s", (subject,))
