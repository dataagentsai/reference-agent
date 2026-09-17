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

from support_agent.contracts import ConversationId, Escalation, RunId, StoredSession
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
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None:
        async with self._pool.connection() as conn:
            # One statement, both indexes — F-006. The conversation id goes in
            # the same row rather than a second table, so the two can never
            # disagree and no transaction is needed to keep them together.
            await conn.execute(
                """
                INSERT INTO agent_state.checkpoints
                    (run_id, conversation_id, state, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE
                    SET state = EXCLUDED.state,
                        conversation_id = EXCLUDED.conversation_id,
                        updated_at = now()
                """,
                (run_id, conversation_id, state),
            )

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


class PostgresEscalationStore:
    """One row per escalation. The record that has to outlive the process.

    This is the store whose absence was the whole defect: a customer told that a
    colleague would take over, and nothing anywhere that a colleague could find.
    """

    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def put(self, escalation: Escalation) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.escalations
                    (id, conversation_id, run_id, customer_id, tier, rule_id,
                     rules_version, reason, state, created_at, expires_at,
                     resolved_at, outcome, outcome_by, outcome_note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    state        = EXCLUDED.state,
                    resolved_at  = EXCLUDED.resolved_at,
                    outcome      = EXCLUDED.outcome,
                    outcome_by   = EXCLUDED.outcome_by,
                    outcome_note = EXCLUDED.outcome_note
                """,
                (
                    escalation.id,
                    escalation.conversation_id,
                    escalation.run_id,
                    escalation.customer_id,
                    escalation.tier,
                    escalation.rule_id,
                    escalation.rules_version,
                    escalation.reason,
                    escalation.state.value,
                    escalation.created_at,
                    escalation.expires_at,
                    escalation.resolved_at,
                    escalation.outcome.value if escalation.outcome else None,
                    escalation.outcome_by,
                    escalation.outcome_note,
                ),
            )
            # Only the closing fields are updatable, exactly as for approvals.
            # Why it fired is fixed at raise time — a store that let `rule_id`
            # change afterwards would let the analysis blame the wrong rule.

    async def get(self, escalation_id: str) -> Escalation | None:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM agent_state.escalations WHERE id = %s", (escalation_id,)
            )
            row = await cur.fetchone()
        return Escalation.model_validate(row) if row else None

    async def open_for(self, conversation_id: str) -> Escalation | None:
        """The read on the hot path: every turn of an escalated conversation.

        Ordered and limited rather than asserting uniqueness, so a duplicate that
        should not exist degrades to "the newest one" instead of raising in front
        of a customer.
        """
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT * FROM agent_state.escalations
                WHERE conversation_id = %s AND state = 'queued'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            )
            row = await cur.fetchone()
        return Escalation.model_validate(row) if row else None

    async def pending(self) -> tuple[Escalation, ...]:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT * FROM agent_state.escalations WHERE state = 'queued' ORDER BY created_at"
            )
            rows = await cur.fetchall()
        return tuple(Escalation.model_validate(r) for r in rows)


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
