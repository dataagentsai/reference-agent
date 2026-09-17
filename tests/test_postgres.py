"""Durability, against a real Postgres.

Skipped when no database is reachable, so the suite stays runnable anywhere. The
What is here is what a restart must not lose: the conversation, the ledger and
the escalation queue. Approvals left this file with T-028 — they are Temporal
workflows, and their restart is tested in `test_approvals.py`.
"""

from __future__ import annotations

import os

import pytest

from support_agent import identity as ident
from support_agent.contracts import (
    ConversationId,
    IdempotencyKey,
    Identity,
    Message,
    RunId,
    ToolResult,
)
from support_agent.state import Conversation

psycopg_pool = pytest.importorskip("psycopg_pool")

DSN = os.environ.get("AGENT_DATABASE_URL", "postgresql:///support_agent")
T0 = 1_000_000
HOUR = 3600


async def _pool():
    pool = psycopg_pool.AsyncConnectionPool(DSN, min_size=1, max_size=2, open=False)
    try:
        await pool.open(wait=True, timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database at {DSN}: {exc}")
    return pool


@pytest.fixture
async def pool():
    p = await _pool()
    async with p.connection() as conn:
        await conn.execute("TRUNCATE agent_state.checkpoints, agent_state.idempotency")
    yield p
    await p.close()


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def key(step: int = 2, iteration: int = 1) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId("run_pg"), step=step, iteration=iteration)


def conversation(text: str = "hello") -> Conversation:
    return Conversation(
        conversation_id=ConversationId("cnv_1"),
        customer_id="C-1042",
        messages=(Message(role="user", content=text),),
    )


# --------------------------------------------------------------------------- #
# Checkpoints.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0044")
async def test_a_checkpoint_survives_a_new_store_and_a_new_pool(pool) -> None:
    from support_agent.state.postgres import PostgresCheckpointStore

    await PostgresCheckpointStore(pool).checkpoint(
        RunId("run_a"), conversation().encode(), conversation_id=ConversationId("cnv_1")
    )

    second = await _pool()
    try:
        raw = await PostgresCheckpointStore(second).resume(RunId("run_a"))
    finally:
        await second.close()

    assert raw is not None
    assert Conversation.decode(raw).messages[0].content == "hello"


@pytest.mark.discharges("AHC-0044")
async def test_a_checkpoint_is_never_briefly_absent(pool) -> None:
    """Upsert rather than delete-then-insert: absent is indistinguishable from
    "this run never happened" to anything that resumes."""
    from support_agent.state.postgres import PostgresCheckpointStore

    store = PostgresCheckpointStore(pool)
    await store.checkpoint(
        RunId("run_b"), conversation("first").encode(), conversation_id=ConversationId("cnv_1")
    )
    await store.checkpoint(
        RunId("run_b"), conversation("second").encode(), conversation_id=ConversationId("cnv_1")
    )

    raw = await store.resume(RunId("run_b"))
    assert raw is not None
    assert Conversation.decode(raw).messages[0].content == "second"


@pytest.mark.discharges("AHC-0102")
async def test_resuming_an_unknown_run_is_none(pool) -> None:
    from support_agent.state.postgres import PostgresCheckpointStore

    assert await PostgresCheckpointStore(pool).resume(RunId("never")) is None


# --------------------------------------------------------------------------- #
# The ledger. Correctness is the primary key, not the code.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0102")
async def test_the_ledger_round_trips(pool) -> None:
    from support_agent.idempotency.postgres import PostgresLedger

    ledger = PostgresLedger(pool)
    assert await ledger.seen(key()) is None
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"refund_id": "rf_1"}))

    stored = await ledger.seen(key())
    assert stored is not None
    assert stored.structured["refund_id"] == "rf_1"


@pytest.mark.discharges("AAC-0047")
async def test_two_writers_racing_leave_one_row(pool) -> None:
    """`ON CONFLICT DO NOTHING`. An application-level check-then-insert has a
    window between the check and the insert, and the window is exactly where a
    double refund lives."""
    import asyncio

    from support_agent.idempotency.postgres import PostgresLedger

    ledger = PostgresLedger(pool)
    await asyncio.gather(
        *(
            ledger.record(key(), ToolResult(name="issue_refund", structured={"attempt": n}))
            for n in range(5)
        )
    )

    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT count(*) FROM agent_state.idempotency")).fetchone()
    assert row[0] == 1


@pytest.mark.discharges("AHC-0074", "AAC-0047", "AHC-0102")
async def test_the_first_outcome_for_a_key_is_the_outcome(pool) -> None:
    from support_agent.idempotency.postgres import PostgresLedger

    ledger = PostgresLedger(pool)
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"which": "first"}))
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"which": "second"}))

    stored = await ledger.seen(key())
    assert stored is not None and stored.structured["which"] == "first"


# B2's gate — an approval outlives the process that raised it — moved to
# `test_approvals.py` with T-028. The approval is a Temporal workflow now, and
# the restart it survives is the *worker's*, not this database's.


# --------------------------------------------------------------------------- #
# Durability is declared, not assumed.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0102")
def test_every_postgres_store_declares_itself_durable() -> None:
    from support_agent.idempotency.postgres import PostgresLedger
    from support_agent.state.postgres import PostgresCheckpointStore, PostgresSessionStore

    for cls in (PostgresCheckpointStore, PostgresLedger, PostgresSessionStore):
        assert cls.durable is True


# Escalations left this file with T-028, as approvals did. They are Temporal
# workflows now: what survives a restart is tested against a real server in
# `test_temporal_live.py`, the queue and the outcome through the desk in
# `test_reviewer.py`, and the lapse — a sweeper here — is the workflow's timer.
