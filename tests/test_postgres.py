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
        await conn.execute("TRUNCATE agent_state.escalations")
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


@pytest.mark.discharges("AAC-0056", "AAC-0047", "AHC-0057", "ext:approval_queue")
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


@pytest.mark.discharges("AAC-0056")
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


@pytest.mark.discharges("P-APPROVAL-QUEUE")
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


@pytest.mark.discharges("AAC-0056", "AHC-0057")
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


@pytest.mark.discharges("AHC-0102")
def test_every_postgres_store_declares_itself_durable() -> None:
    from support_agent.idempotency.postgres import PostgresLedger
    from support_agent.state.postgres import (
        PostgresApprovalStore,
        PostgresCheckpointStore,
        PostgresEscalationStore,
    )

    for cls in (
        PostgresCheckpointStore,
        PostgresLedger,
        PostgresApprovalStore,
        PostgresEscalationStore,
    ):
        assert cls.durable is True


# --------------------------------------------------------------------------- #
# Escalations. The store that was written and never once constructed.
# --------------------------------------------------------------------------- #


def pg_escalations(pool):
    from support_agent.state.postgres import PostgresEscalationStore

    return PostgresEscalationStore(pool)


async def raised(store, *, rule_id: str = "asked-for-human", ttl_s: int = HOUR, now: int = T0):
    from support_agent import escalation as esc

    return await esc.raise_for(
        store,
        conversation_id="cnv_1",
        run_id="run_pg",
        customer_id="C-1042",
        reason="the customer asked for a human",
        rule_id=rule_id,
        rules_version="v1",
        ttl_s=ttl_s,
        now=now,
    )


@pytest.mark.discharges("op:escalate")
async def test_an_escalation_outlives_the_process_that_raised_it(pool) -> None:
    """The whole point of the record, and the reason `InMemoryEscalationStore`
    says `durable = False` out loud.

    A customer is told a colleague will pick this up. If the process that made
    that promise takes the promise with it, the customer was lied to by an
    implementation detail.
    """
    row = await raised(pg_escalations(pool))

    second = await _pool()
    try:
        found = await pg_escalations(second).get(row.id)
    finally:
        await second.close()

    assert found is not None
    assert found.id == row.id
    assert found.rule_id == "asked-for-human"
    assert found.tier == 1
    assert found.created_at == T0, "epoch seconds survive the round trip as integers"


@pytest.mark.discharges("P-ESC-QUEUE")
async def test_the_queue_a_reviewer_reads_is_ordered_and_open_only(pool) -> None:
    from support_agent import escalation as esc
    from support_agent.contracts import EscalationOutcome

    store = pg_escalations(pool)
    first = await raised(store, rule_id="asked-for-human", now=T0)
    second = await raised(store, rule_id="lost-in-transit", now=T0 + 10)
    third = await raised(store, rule_id="second-refusal", now=T0 + 20)

    await esc.resolve(
        store, second.id, outcome=EscalationOutcome.RESOLVED, by="desk-3", now=T0 + 30
    )

    assert [e.id for e in await store.pending()] == [first.id, third.id], "oldest first, open only"


@pytest.mark.discharges("P-ESC-OWNS")
async def test_open_for_finds_the_conversations_live_escalation(pool) -> None:
    """The hot path: every turn of an escalated conversation asks whether one is
    open. Keyed by conversation because that is the handle the agent holds."""
    store = pg_escalations(pool)
    row = await raised(store)

    assert (await store.open_for("cnv_1")).id == row.id
    assert await store.open_for("cnv_nothing") is None


@pytest.mark.discharges("P-ESC-OUTCOME")
async def test_only_the_closing_fields_are_updatable(pool) -> None:
    """Why it fired is fixed at raise time. A store that let `rule_id` change
    afterwards would let the analysis blame the wrong rule — the same reasoning
    that keeps an approval's action immutable."""
    from support_agent.contracts import EscalationState

    store = pg_escalations(pool)
    row = await raised(store, rule_id="asked-for-human")

    tampered = row.model_copy(
        update={
            "rule_id": "something-else",
            "reason": "rewritten",
            "tier": 2,
            "state": EscalationState.RESOLVED,
        }
    )
    await store.put(tampered)

    stored = await store.get(row.id)
    assert stored is not None
    assert (stored.rule_id, stored.reason, stored.tier) == ("asked-for-human", row.reason, 1)
    assert stored.state is EscalationState.RESOLVED, "the closing fields did change"


@pytest.mark.discharges("P-ESC-OUTCOME")
async def test_the_outcome_survives_and_is_sliceable_by_rule(pool) -> None:
    """The row the over-escalation rate is computed from. If this does not
    round-trip, the number cannot be computed at all."""
    from support_agent import escalation as esc
    from support_agent.contracts import EscalationOutcome

    store = pg_escalations(pool)
    row = await raised(store, rule_id="second-refusal")
    await esc.resolve(
        store,
        row.id,
        outcome=EscalationOutcome.AGENT_COULD_HAVE,
        by="desk-7",
        note="ordinary status question",
        now=T0 + 60,
    )

    closed = await store.get(row.id)
    assert closed is not None
    assert closed.outcome is EscalationOutcome.AGENT_COULD_HAVE
    assert (closed.outcome_by, closed.outcome_note) == ("desk-7", "ordinary status question")
    assert closed.rule_id == "second-refusal"
    assert closed.resolved_at == T0 + 60


@pytest.mark.discharges("P-ESC-OUTCOME")
async def test_the_database_refuses_an_outcome_the_model_does_not_know(pool) -> None:
    """Belt and braces, the same shape as `refunds.idempotency_key UNIQUE`: the
    application coerces at the boundary, and the constraint makes the bad row
    impossible even if that check is somehow bypassed."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO agent_state.escalations
                    (id, conversation_id, run_id, customer_id, tier, rule_id,
                     rules_version, reason, state, created_at, expires_at, outcome)
                VALUES ('E-BAD','c','r','C-1042',1,'x','v1','y','queued',1,2,'sorted-it')
                """
            )


@pytest.mark.discharges("op:escalate")
async def test_the_sweeper_lapses_across_a_restart(pool) -> None:
    """Expiry is a property of the row, not of the process that wrote it — which
    is exactly the case the in-memory store cannot answer."""
    from support_agent import escalation as esc
    from support_agent.contracts import EscalationState

    store = pg_escalations(pool)
    stale = await raised(store, ttl_s=60, now=T0)

    second = await _pool()
    try:
        lapsed = await esc.sweep(pg_escalations(second), now=T0 + HOUR)
    finally:
        await second.close()

    assert [e.id for e in lapsed] == [stale.id]
    assert (await store.get(stale.id)).state is EscalationState.EXPIRED
    assert await store.pending() == ()
