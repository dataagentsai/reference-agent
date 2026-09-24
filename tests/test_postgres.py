"""Durability, against a real Postgres.

Skipped when no database is reachable, so the suite stays runnable anywhere. The
What is here is what a restart must not lose: the conversation, the ledger and
the escalation queue. Approvals left this file with T-028 — they are Temporal
workflows, and their restart is tested in `test_approvals.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest

from support_agent import identity as ident
from support_agent.contracts import (
    ConversationId,
    IdempotencyKey,
    Identity,
    Message,
    RunId,
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


def named(what: str) -> str:
    """A name of this test's own. The store is durable, which is the point,
    so a name shared between tests is answered before the second one runs."""
    return f"{uuid.uuid4().hex[:8]}:{what}"


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
# The requests store — T-062. Correctness is the statement, not the code.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0102")
async def test_a_claim_round_trips(pool) -> None:
    from support_agent import requests as req
    from support_agent.requests.postgres import PostgresRequests

    store = PostgresRequests(pool)
    name = named("round-trip")
    held = await store.claim(name, scope=req.Scope.TOOL)
    assert held.name == name
    await store.settle(name, {"refund_id": "rf_1"})

    with pytest.raises(req.AlreadyAnswered) as answered:
        await store.claim(name, scope=req.Scope.TOOL)
    assert answered.value.outcome == {"refund_id": "rf_1"}


@pytest.mark.discharges("AAC-0047")
async def test_two_callers_racing_for_one_name_leave_one_holder(pool) -> None:
    """The `WHERE` on the conflict branch is the whole guard. An
    application-level check-then-claim has a window between the two, and the
    window is exactly where a double refund lives."""
    import asyncio

    from support_agent import requests as req
    from support_agent.requests.postgres import PostgresRequests

    store = PostgresRequests(pool)
    name = named("racing")
    taken = await asyncio.gather(
        *(store.claim(name, scope=req.Scope.TOOL) for _ in range(5)),
        return_exceptions=True,
    )

    held = [t for t in taken if not isinstance(t, BaseException)]
    assert len(held) == 1, "exactly one caller holds it; the rest are refused"
    assert all(isinstance(t, req.StillRunning) for t in taken if isinstance(t, BaseException))


@pytest.mark.discharges("AHC-0074", "AAC-0047", "AHC-0102")
async def test_the_first_definite_answer_is_the_answer(pool) -> None:
    from support_agent import requests as req
    from support_agent.requests.postgres import PostgresRequests

    store = PostgresRequests(pool)
    name = named("first-wins")
    await store.claim(name, scope=req.Scope.TOOL)
    await store.settle(name, {"which": "first"})
    await store.settle(name, {"which": "second"})

    with pytest.raises(req.AlreadyAnswered) as answered:
        await store.claim(name, scope=req.Scope.TOOL)
    assert answered.value.outcome == {"which": "first"}


@pytest.mark.discharges("AHC-0074", "AHC-0102")
async def test_an_abandoned_name_is_free_again(pool) -> None:
    """Indefinite: nothing is stored and the name goes back, so a retry reaches
    the far end under the same name for it to recognise."""
    from support_agent import requests as req
    from support_agent.requests.postgres import PostgresRequests

    store = PostgresRequests(pool)
    name = named("abandoned")
    await store.claim(name, scope=req.Scope.TOOL)
    await store.abandon(name)

    assert (await store.claim(name, scope=req.Scope.TOOL)).name == name


@pytest.mark.discharges("AHC-0102")
def test_every_postgres_store_declares_itself_durable() -> None:
    from support_agent.requests.postgres import PostgresRequests
    from support_agent.state.postgres import PostgresCheckpointStore, PostgresSessionStore

    for cls in (PostgresCheckpointStore, PostgresRequests, PostgresSessionStore):
        assert cls.durable is True


# Escalations left this file with T-028, as approvals did. They are Temporal
# workflows now: what survives a restart is tested against a real server in
# `test_temporal_live.py`, the queue and the outcome through the desk in
# `test_reviewer.py`, and the lapse — a sweeper here — is the workflow's timer.
