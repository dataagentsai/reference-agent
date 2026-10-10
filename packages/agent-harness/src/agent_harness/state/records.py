"""Our own approval and escalation records, in memory and in PostgreSQL (A3).

Realises `contracts.records`. Each write is an upsert keyed by the wait's id:
the wait writes its record in a checkpointed step, and a step re-run after a
crash writes the same row again, which changes nothing but `updated_at`.

The tables are the agent's to create, beside `agent_state.checkpoints`
(claims-fnol-azure F-24): the reference agent's `sql/002_records.sql` and
claims-fnol-azure's `migrations/002_records.sql` declare them.

Timestamps are whole seconds on the records, as on `Approval`, and
`timestamptz` in the tables, so a person reading a row reads a time.
"""

from __future__ import annotations

import time
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent_harness.contracts.records import ApprovalRecord, EscalationRecord


class InMemoryRecords:
    """One process, lost on exit: tests and the gates."""

    durable = False

    def __init__(self) -> None:
        self._approvals: dict[str, ApprovalRecord] = {}
        self._escalations: dict[str, EscalationRecord] = {}

    async def put_approval(self, record: ApprovalRecord) -> None:
        self._approvals[record.id] = record.model_copy(update={"updated_at": int(time.time())})

    async def approval(self, approval_id: str) -> ApprovalRecord | None:
        return self._approvals.get(approval_id)

    async def put_escalation(self, record: EscalationRecord) -> None:
        self._escalations[record.id] = record.model_copy(update={"updated_at": int(time.time())})

    async def escalation(self, escalation_id: str) -> EscalationRecord | None:
        return self._escalations.get(escalation_id)


_PUT_APPROVAL = """
INSERT INTO agent_state.approvals
    (id, action, args, args_digest, requested_for, conversation_id, idempotency_key,
     decided_by, expires_at, status, reason, created_at, updated_at)
VALUES (%(id)s, %(action)s, %(args)s, %(args_digest)s, %(requested_for)s,
        %(conversation_id)s, %(idempotency_key)s, %(decided_by)s,
        to_timestamp(%(expires_at)s), %(status)s, %(reason)s,
        to_timestamp(%(created_at)s), now())
ON CONFLICT (id) DO UPDATE SET
    action = EXCLUDED.action, args = EXCLUDED.args, args_digest = EXCLUDED.args_digest,
    requested_for = EXCLUDED.requested_for, conversation_id = EXCLUDED.conversation_id,
    idempotency_key = EXCLUDED.idempotency_key, decided_by = EXCLUDED.decided_by,
    expires_at = EXCLUDED.expires_at, status = EXCLUDED.status, reason = EXCLUDED.reason,
    updated_at = now()
"""

_GET_APPROVAL = """
SELECT id, action, args, args_digest, requested_for, conversation_id, idempotency_key,
       decided_by, extract(epoch FROM expires_at)::bigint AS expires_at, status, reason,
       extract(epoch FROM created_at)::bigint AS created_at,
       extract(epoch FROM updated_at)::bigint AS updated_at
  FROM agent_state.approvals WHERE id = %s
"""

_PUT_ESCALATION = """
INSERT INTO agent_state.escalations
    (id, conversation_id, requested_for, question, found, missing, assignee,
     sla_due_at, status, created_at, updated_at)
VALUES (%(id)s, %(conversation_id)s, %(requested_for)s, %(question)s, %(found)s,
        %(missing)s, %(assignee)s, to_timestamp(%(sla_due_at)s), %(status)s,
        to_timestamp(%(created_at)s), now())
ON CONFLICT (id) DO UPDATE SET
    conversation_id = EXCLUDED.conversation_id, requested_for = EXCLUDED.requested_for,
    question = EXCLUDED.question, found = EXCLUDED.found, missing = EXCLUDED.missing,
    assignee = EXCLUDED.assignee, sla_due_at = EXCLUDED.sla_due_at,
    status = EXCLUDED.status, updated_at = now()
"""

_GET_ESCALATION = """
SELECT id, conversation_id, requested_for, question, found, missing, assignee,
       extract(epoch FROM sla_due_at)::bigint AS sla_due_at, status,
       extract(epoch FROM created_at)::bigint AS created_at,
       extract(epoch FROM updated_at)::bigint AS updated_at
  FROM agent_state.escalations WHERE id = %s
"""


class PostgresRecords:
    """`agent_state.approvals` and `agent_state.escalations`, one statement a write."""

    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def put_approval(self, record: ApprovalRecord) -> None:
        row = record.model_dump()
        await self._write(_PUT_APPROVAL, {**row, "args": Jsonb(row["args"])})

    async def approval(self, approval_id: str) -> ApprovalRecord | None:
        row = await self._read(_GET_APPROVAL, approval_id)
        return None if row is None else ApprovalRecord(**row)

    async def put_escalation(self, record: EscalationRecord) -> None:
        await self._write(_PUT_ESCALATION, record.model_dump())

    async def escalation(self, escalation_id: str) -> EscalationRecord | None:
        row = await self._read(_GET_ESCALATION, escalation_id)
        return None if row is None else EscalationRecord(**row)

    async def _write(self, sql: str, params: dict[str, Any]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(sql, params)

    async def _read(self, sql: str, key: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(sql, (key,))
            found: dict[str, Any] | None = await cur.fetchone()
        return found


__all__ = ["InMemoryRecords", "PostgresRecords"]
