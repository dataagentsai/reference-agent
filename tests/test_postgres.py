"""Durability, against a real Postgres.

Skipped when no database is reachable, so the suite stays runnable anywhere. The
gate for B2 is the last test: an approval outlives the process that raised it.
"""

from __future__ import annotations

import os

import pytest

from support_agent import approvals as ap
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
        await conn.execute("TRUNCATE agent_state.approvals")
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


async def test_a_checkpoint_survives_a_new_store_and_a_new_pool(pool) -> None:
    from support_agent.state.postgres import PostgresCheckpointStore

    await PostgresCheckpointStore(pool).checkpoint(RunId("run_a"), conversation().encode())

    second = await _pool()
    try:
        raw = await PostgresCheckpointStore(second).resume(RunId("run_a"))
    finally:
        await second.close()

    assert raw is not None
    assert Conversation.decode(raw).messages[0].content == "hello"


async def test_a_checkpoint_is_never_briefly_absent(pool) -> None:
    """Upsert rather than delete-then-insert: absent is indistinguishable from
    "this run never happened" to anything that resumes."""
    from support_agent.state.postgres import PostgresCheckpointStore

    store = PostgresCheckpointStore(pool)
    await store.checkpoint(RunId("run_b"), conversation("first").encode())
    await store.checkpoint(RunId("run_b"), conversation("second").encode())

    raw = await store.resume(RunId("run_b"))
    assert raw is not None
    assert Conversation.decode(raw).messages[0].content == "second"


async def test_resuming_an_unknown_run_is_none(pool) -> None:
    from support_agent.state.postgres import PostgresCheckpointStore

    assert await PostgresCheckpointStore(pool).resume(RunId("never")) is None


# --------------------------------------------------------------------------- #
# The ledger. Correctness is the primary key, not the code.
# --------------------------------------------------------------------------- #


async def test_the_ledger_round_trips(pool) -> None:
    from support_agent.idempotency.postgres import PostgresLedger

    ledger = PostgresLedger(pool)
    assert await ledger.seen(key()) is None
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"refund_id": "rf_1"}))

    stored = await ledger.seen(key())
    assert stored is not None
    assert stored.structured["refund_id"] == "rf_1"


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


async def test_the_first_outcome_for_a_key_is_the_outcome(pool) -> None:
    from support_agent.idempotency.postgres import PostgresLedger

    ledger = PostgresLedger(pool)
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"which": "first"}))
    await ledger.record(key(), ToolResult(name="issue_refund", structured={"which": "second"}))

    stored = await ledger.seen(key())
    assert stored is not None and stored.structured["which"] == "first"


# --------------------------------------------------------------------------- #
# B2's gate: an approval outlives the process that raised it.
# --------------------------------------------------------------------------- #


async def test_an_approval_survives_a_process_restart(pool) -> None:
    """The whole reason P6 exists.

    A human takes an hour to decide. The process that asked is long gone. The
    grant must still execute under the *original* idempotency key, or the
    restart becomes a second refund.
    """
    from support_agent.state.postgres import PostgresApprovalStore

    approval = await ap.request(
        PostgresApprovalStore(pool),
        action=ap.REFUND_ACTION,
        args={"order_id": "AB-1", "amount": "12400"},
        reason="above threshold",
        identity=customer(),
        idempotency_key=key(),
        now=T0,
    )

    # Everything above is discarded — a new pool and a new store, as close to a
    # restart as a test can stage.
    reborn = await _pool()
    try:
        store = PostgresApprovalStore(reborn)
        recovered = await store.get(approval.id)
        assert recovered is not None
        assert not recovered.decided

        granted = await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + HOUR)
        assert ap.is_executable(granted, now=T0 + HOUR)
        assert ap.stored_key(granted) == key()

        elevated = ap.granted_identity(granted, customer(), now=T0 + HOUR)
        assert elevated.may(ident.SCOPE_REFUNDS_WRITE)
    finally:
        await reborn.close()


async def test_a_decision_is_terminal_across_processes(pool) -> None:
    """The rule has to hold in the store, not only in the module that wrote it."""
    from support_agent.state.postgres import PostgresApprovalStore

    store = PostgresApprovalStore(pool)
    approval = await ap.request(
        store,
        action=ap.REFUND_ACTION,
        args={"amount": "12400"},
        reason="above threshold",
        identity=customer(),
        idempotency_key=key(),
        now=T0,
    )
    await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + HOUR)

    other = await _pool()
    try:
        with pytest.raises(ap.ApprovalError, match="already decided"):
            await ap.decide(
                PostgresApprovalStore(other), approval.id, granted=False, by="ops-9", now=T0 + HOUR
            )
    finally:
        await other.close()


async def test_the_pending_queue_is_what_a_reviewer_sees(pool) -> None:
    from support_agent.state.postgres import PostgresApprovalStore

    store = PostgresApprovalStore(pool)
    first = await ap.request(
        store,
        action=ap.REFUND_ACTION,
        args={"amount": "12400"},
        reason="r",
        identity=customer(),
        idempotency_key=key(1, 0),
        now=T0,
    )
    await ap.request(
        store,
        action=ap.REFUND_ACTION,
        args={"amount": "20000"},
        reason="r",
        identity=customer(),
        idempotency_key=key(2, 0),
        now=T0 + 1,
    )

    assert len(await store.pending()) == 2
    await ap.decide(store, first.id, granted=True, by="ops-7", now=T0 + HOUR)
    remaining = await store.pending()
    assert len(remaining) == 1
    assert remaining[0].id != first.id


async def test_the_action_and_key_cannot_change_after_the_request(pool) -> None:
    """An approval granted for one thing must not be executable as another."""
    from support_agent.state.postgres import PostgresApprovalStore

    store = PostgresApprovalStore(pool)
    approval = await ap.request(
        store,
        action=ap.REFUND_ACTION,
        args={"amount": "12400"},
        reason="r",
        identity=customer(),
        idempotency_key=key(),
        now=T0,
    )
    tampered = approval.model_copy(
        update={"action": "dispatch_replacement", "args": {"amount": "1"}}
    )
    await store.put(tampered)

    stored = await store.get(approval.id)
    assert stored is not None
    assert stored.action == ap.REFUND_ACTION
    assert stored.args == {"amount": "12400"}


# --------------------------------------------------------------------------- #
# Durability is declared, not assumed.
# --------------------------------------------------------------------------- #


def test_every_postgres_store_declares_itself_durable() -> None:
    from support_agent.idempotency.postgres import PostgresLedger
    from support_agent.state.postgres import PostgresApprovalStore, PostgresCheckpointStore

    for cls in (PostgresCheckpointStore, PostgresLedger, PostgresApprovalStore):
        assert cls.durable is True
