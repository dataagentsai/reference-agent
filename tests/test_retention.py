"""Q-RETENTION — every record the harness writes is kept 30 days, then deleted.

AAC-0095 asks that retention be bounded; the owner bounded it at 30 days on
2026-09-26 (T-072). Erasure asks *whose*, this asks *how old*, and each store
answers through one method on its seam, `expire(before)`, with the moment given
by the caller rather than read from the wall.

Table-driven throughout: each row is a record written at some age, and whether
it must survive a cut-off 30 days back. The Postgres rows are in
`test_postgres.py`, beside the other durable-store tests.

**The ledger is the row to read.** Erasure redacts it, because a settled
refund's name must keep refusing a replay. Expiry deletes it, because nothing
re-presents a name that old — and a live claim is never removed, whatever its
age.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from support_agent import requests as req
from support_agent.config import Settings
from support_agent.contracts import ConversationId, RunId, StoredSession
from support_agent.erasure import retention
from support_agent.state import FileCheckpointStore, InMemoryCheckpointStore, InMemorySessionStore

DAY = retention.DAY_S
NOW = 400 * DAY
"""An arbitrary 'today', far enough from zero that 30 days back is positive."""
BEFORE = retention.cutoff(NOW)


class Clock:
    """A clock the test moves. The stores stamp what they write with it."""

    def __init__(self, at: int = 0) -> None:
        self.at = at

    def __call__(self) -> int:
        return self.at


def written(days_ago: float) -> int:
    return int(NOW - days_ago * DAY)


# (why, days since written, survives)
AGES = [
    ("a month and a day old goes", 31, False),
    ("one second past the window goes", 30 + 1 / DAY, False),
    ("exactly thirty days old is kept", 30, True),
    ("yesterday is kept", 1, True),
    ("written today is kept", 0, True),
]


# --------------------------------------------------------------------------- #
# Conversations: memory and file, one table.
# --------------------------------------------------------------------------- #


def make_store(kind: str, clock: Clock, tmp_path: Path):
    if kind == "memory":
        return InMemoryCheckpointStore(clock=clock)
    return FileCheckpointStore(tmp_path, clock=clock)


@pytest.mark.discharges("Q-RETENTION", "AAC-0095")
@pytest.mark.parametrize("kind", ["memory", "file"])
@pytest.mark.parametrize(("why", "age", "survives"), AGES, ids=[a[0] for a in AGES])
async def test_a_turn_is_kept_for_the_window_and_no_longer(
    kind: str, why: str, age: float, survives: bool, tmp_path: Path
) -> None:
    clock = Clock(written(age))
    store = make_store(kind, clock, tmp_path)
    await store.checkpoint(
        RunId("run_a"), b"{}", conversation_id=ConversationId("cnv_a"), customer_id="C-1"
    )

    gone = await store.expire(BEFORE)

    assert gone == (0 if survives else 1), why
    assert (await store.resume(RunId("run_a")) is not None) is survives, why
    assert (await store.latest(ConversationId("cnv_a")) is not None) is survives, why


@pytest.mark.discharges("Q-RETENTION")
@pytest.mark.parametrize("kind", ["memory", "file"])
async def test_an_active_conversation_loses_its_old_turns_and_keeps_its_newest(
    kind: str, tmp_path: Path
) -> None:
    """Judged per record, not per conversation: a conversation that ran for two
    months keeps its last month, and is resumable from its newest turn."""
    clock = Clock(written(45))
    store = make_store(kind, clock, tmp_path)
    conversation = ConversationId("cnv_long")
    await store.checkpoint(RunId("run_old"), b'{"n": 1}', conversation_id=conversation)
    clock.at = written(2)
    await store.checkpoint(RunId("run_new"), b'{"n": 2}', conversation_id=conversation)

    assert await store.expire(BEFORE) == 1

    assert await store.resume(RunId("run_old")) is None
    assert await store.latest(conversation) == b'{"n": 2}'


@pytest.mark.discharges("Q-RETENTION", "AAC-0117")
async def test_an_expired_turn_is_gone_from_erasure_too(tmp_path: Path) -> None:
    """The index `forget` reads goes with the state, so an erasure after expiry
    reports nothing rather than a run it can no longer find."""
    store = FileCheckpointStore(tmp_path, clock=Clock(written(40)))
    await store.checkpoint(RunId("r"), b"{}", conversation_id=ConversationId("c"), customer_id="X")

    await store.expire(BEFORE)

    assert list(tmp_path.glob("*.json")) == []
    assert await store.forget("X") == ()


# --------------------------------------------------------------------------- #
# The ledger: deleted, not tombstoned — and a live claim never.
# --------------------------------------------------------------------------- #

# (why, days since recorded, answered, lease lapsed, survives)
LEDGER = [
    ("an answered name past the window goes", 31, True, True, False),
    ("an answered name inside it stays, and still refuses", 29, True, True, True),
    ("a lapsed claim past the window goes", 31, False, True, False),
    ("a claim still held stays, however old", 31, False, False, True),
    ("a lapsed claim inside the window stays", 1, False, True, True),
]


@pytest.mark.discharges("Q-RETENTION", "AAC-0095", "AHC-0074")
@pytest.mark.parametrize(
    ("why", "age", "answered", "lapsed", "survives"), LEDGER, ids=[r[0] for r in LEDGER]
)
async def test_the_ledger_expires_what_nothing_can_repeat(
    why: str, age: int, answered: bool, lapsed: bool, survives: bool
) -> None:
    lease = Clock(0)
    ledger = req.InMemoryRequests(now=lease, clock=Clock(written(age)))
    await ledger.claim("run_x:1:0", scope=req.Scope.TOOL)
    if answered:
        await ledger.settle("run_x:1:0", {"text": "refunded"})
    if lapsed:
        lease.at = req.CLAIM_TTL_S + 1

    assert await ledger.expire(BEFORE) == (0 if survives else 1), why

    if not survives:
        # Deleted: the name is free. Safe only because nothing that old repeats
        # it — the argument `retention.SHORTEST_WINDOW_DAYS` enforces.
        await ledger.claim("run_x:1:0", scope=req.Scope.TOOL)
    elif answered:
        with pytest.raises(req.AlreadyAnswered):
            await ledger.claim("run_x:1:0", scope=req.Scope.TOOL)
    elif not lapsed:
        with pytest.raises(req.StillRunning):
            await ledger.claim("run_x:1:0", scope=req.Scope.TOOL)


@pytest.mark.discharges("Q-RETENTION")
async def test_a_redacted_name_is_dated_from_its_claim_not_its_redaction() -> None:
    """Erasure rewrites the row; it must not make a month-old name look new."""
    ledger = req.InMemoryRequests(clock=Clock(written(40)))
    async with req.once(ledger, "run_r:1:0", scope=req.Scope.TOOL) as claim:
        claim.outcome = {"text": "delivered"}
    await ledger.redact((RunId("run_r"),))

    assert await ledger.expire(BEFORE) == 1


# --------------------------------------------------------------------------- #
# Stored logins.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("Q-RETENTION", "AAC-0095")
@pytest.mark.parametrize(("why", "age", "survives"), AGES, ids=[a[0] for a in AGES])
async def test_a_login_not_refreshed_within_the_window_goes(
    why: str, age: float, survives: bool
) -> None:
    sessions = InMemorySessionStore()
    await sessions.put(StoredSession(subject="s", refresh_token="t", updated_at=written(age)))

    assert await sessions.expire(BEFORE) == (0 if survives else 1), why
    assert (await sessions.get("s") is not None) is survives, why


# --------------------------------------------------------------------------- #
# One call, every store; and the window itself.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("Q-RETENTION", "AAC-0095")
async def test_one_pass_reaches_every_store_and_counts_each() -> None:
    old, young = Clock(written(40)), Clock(written(3))
    moving = Clock(old())
    checkpoints = InMemoryCheckpointStore(clock=moving)
    await checkpoints.checkpoint(RunId("r1"), b"{}", conversation_id=ConversationId("c1"))
    moving.at = young()
    await checkpoints.checkpoint(RunId("r2"), b"{}", conversation_id=ConversationId("c2"))
    ledger = req.InMemoryRequests(clock=old)
    async with req.once(ledger, "r1:1:0", scope=req.Scope.TOOL) as claim:
        claim.outcome = {"ok": True}
    sessions = InMemorySessionStore()
    await sessions.put(StoredSession(subject="a", refresh_token="t", updated_at=old()))
    await sessions.put(StoredSession(subject="b", refresh_token="t", updated_at=young()))

    done = await retention.expire(
        now=NOW, checkpoints=checkpoints, requests=ledger, sessions=sessions
    )

    assert (done.runs, done.names, done.sessions, done.before) == (1, 1, 1, BEFORE)
    assert await checkpoints.latest(ConversationId("c2")) is not None


# (why, days, refused)
WINDOWS = [
    ("the decided thirty days", 30, False),
    ("the floor itself", retention.SHORTEST_WINDOW_DAYS, False),
    ("a day, which would delete a grant's name while it may still run", 1, True),
    ("zero, which would delete everything", 0, True),
]


@pytest.mark.discharges("Q-RETENTION", "AHC-0074")
@pytest.mark.parametrize(("why", "days", "refused"), WINDOWS, ids=[w[0] for w in WINDOWS])
def test_a_window_short_enough_to_reopen_a_replay_is_refused(
    why: str, days: int, refused: bool
) -> None:
    if refused:
        with pytest.raises(retention.WindowTooShort):
            retention.cutoff(NOW, days)
    else:
        assert retention.cutoff(NOW, days) == NOW - days * DAY, why


PROFILE = Path(__file__).resolve().parents[1] / "harness-profile.yaml"


@pytest.mark.discharges("Q-RETENTION")
def test_one_number_in_three_places() -> None:
    """The code's constant, the deployment's setting, and the profile's
    threshold. The profile row is added by hand; until it is, this says so."""
    assert Settings.model_fields["retention_days"].default == retention.RETENTION_DAYS
    thresholds = yaml.safe_load(PROFILE.read_text()).get("thresholds") or {}
    if "retention_days" not in thresholds:
        pytest.skip("harness-profile.yaml has no thresholds.retention_days yet")
    assert thresholds["retention_days"] == retention.RETENTION_DAYS
