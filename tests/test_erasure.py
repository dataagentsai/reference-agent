"""F-056 — a person asks to be forgotten, and something happens.

The finding was found from outside: a specification for a different agent said
*the answer cannot be "the vector store does not support that"*, and this system
had three durable stores with one `DELETE` between them, which was logout.

Two things make this harder than a `DELETE` per table, and both are pinned here:

**The stores have to be asked in one order.** The ledger is keyed by run, and
run ids live only on the conversation rows. Erase conversations first and the
ledger rows become unfindable — so `forget` erases conversations first *and
takes the run ids back from them*, which is the only ordering that works.

**Erasing the ledger is wrong either way**, so it does neither. Deleting the
rows makes every one of those calls executable again; keeping them keeps the
far end's reply about a person who asked to be forgotten. The name stays, the
answer goes, and the duplicate guard is untouched because the guard was never
the stored body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support_agent import erasure
from support_agent import requests as req
from support_agent.contracts import ConversationId, RunId, StoredSession
from support_agent.state import (
    FileCheckpointStore,
    InMemoryCheckpointStore,
    InMemorySessionStore,
)

THEIRS = "C-1042"
SOMEBODY_ELSE = "C-2001"


async def two_turns(store, customer: str = THEIRS, prefix: str = "r") -> list[RunId]:
    """Two turns of one conversation, as the entrypoint records them."""
    runs = [RunId(f"{prefix}-1"), RunId(f"{prefix}-2")]
    for run in runs:
        await store.checkpoint(
            run,
            b'{"said": "my card number is 4111"}',
            conversation_id=ConversationId(f"conv-{customer}"),
            customer_id=customer,
        )
    return runs


async def ledger_for(runs: list[RunId]) -> req.InMemoryRequests:
    """A ledger row per run, each with a definite answer stored."""
    log = req.InMemoryRequests()
    for run in runs:
        async with req.once(log, f"{run}:1:0", scope=req.Scope.TOOL) as claim:
            claim.outcome = {"name": "get_order", "text": "delivered to 4 Elm Road"}
    return log


# --------------------------------------------------------------------------- #
# The stores, each on its own.
# --------------------------------------------------------------------------- #

STORES = ["memory", "file"]


@pytest.fixture(params=STORES)
def checkpoints(request, tmp_path: Path):
    """Both non-Postgres stores, through one set of tests.

    Parametrized rather than written twice because the obligation is on the
    seam: a deployment swaps these, and an erasure that worked in memory and
    silently did nothing on disk is exactly the failure this is guarding.
    """
    if request.param == "memory":
        return InMemoryCheckpointStore()
    return FileCheckpointStore(tmp_path)


@pytest.mark.discharges("AAC-0117", "AHC-0102")
async def test_their_turns_go_and_the_runs_come_back(checkpoints) -> None:
    runs = await two_turns(checkpoints)
    assert await checkpoints.latest(ConversationId(f"conv-{THEIRS}")) is not None

    gone = await checkpoints.forget(THEIRS)

    assert sorted(gone) == sorted(runs), "the runs are the handle on everything else"
    assert await checkpoints.latest(ConversationId(f"conv-{THEIRS}")) is None
    for run in runs:
        assert await checkpoints.resume(run) is None


@pytest.mark.discharges("AAC-0117")
async def test_nobody_elses_turns_go(checkpoints) -> None:
    """The failure that would matter most, and the one a test is cheapest for."""
    await two_turns(checkpoints, THEIRS, prefix="a")
    await two_turns(checkpoints, SOMEBODY_ELSE, prefix="b")

    await checkpoints.forget(THEIRS)

    assert await checkpoints.latest(ConversationId(f"conv-{SOMEBODY_ELSE}")) is not None
    assert await checkpoints.latest(ConversationId(f"conv-{THEIRS}")) is None


@pytest.mark.discharges("AAC-0117")
async def test_an_empty_customer_erases_nothing(checkpoints) -> None:
    """A blank request must not match every unattributed row.

    The one input where being wrong costs the whole table, and the one most
    likely to arrive — a caller that did not resolve an identity passes `""`
    rather than noticing it has nothing.
    """
    await two_turns(checkpoints)

    assert await checkpoints.forget("") == ()
    assert await checkpoints.latest(ConversationId(f"conv-{THEIRS}")) is not None


# --------------------------------------------------------------------------- #
# The ledger: the name stays, the answer goes.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0117", "AHC-0074")
async def test_a_redacted_row_still_refuses_a_repeat() -> None:
    """The whole argument for redacting rather than deleting.

    A replay months later — a queue draining, a retry nobody remembered — must
    not run the call again for somebody who asked to be forgotten. What answers
    that is the row, and the row is still there.
    """
    runs = [RunId("r-1")]
    log = await ledger_for(runs)

    assert await log.redact(tuple(runs)) == 1

    with pytest.raises(req.AlreadyAnswered) as refused:
        await log.claim("r-1:1:0", scope=req.Scope.TOOL)
    assert refused.value.outcome is None, "refused, and with nothing to say about it"


@pytest.mark.discharges("AAC-0117")
async def test_only_the_named_runs_are_redacted() -> None:
    log = await ledger_for([RunId("r-1"), RunId("r-2")])

    assert await log.redact((RunId("r-1"),)) == 1

    with pytest.raises(req.AlreadyAnswered) as kept:
        await log.claim("r-2:1:0", scope=req.Scope.TOOL)
    assert kept.value.outcome is not None


@pytest.mark.discharges("AAC-0117")
async def test_redacting_nothing_is_not_redacting_everything() -> None:
    """An empty tuple is what a customer with no turns produces, and it reaches
    here on the ordinary path rather than as a mistake."""
    log = await ledger_for([RunId("r-1")])
    assert await log.redact(()) == 0
    with pytest.raises(req.AlreadyAnswered) as kept:
        await log.claim("r-1:1:0", scope=req.Scope.TOOL)
    assert kept.value.outcome is not None


# --------------------------------------------------------------------------- #
# All three, in the order that works.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0117", "AHC-0102")
async def test_one_call_reaches_every_store_that_holds_them() -> None:
    checkpoints = InMemoryCheckpointStore()
    runs = await two_turns(checkpoints)
    log = await ledger_for(runs)
    sessions = InMemorySessionStore()
    await sessions.put(StoredSession(subject="login-C-1042", refresh_token=b"x", updated_at=0))

    erased = await erasure.forget(
        customer_id=THEIRS,
        checkpoints=checkpoints,
        requests=log,
        sessions=sessions,
        subject="login-C-1042",
    )

    assert (erased.runs, erased.answers, erased.sessions) == (2, 2, 1)
    assert erased.anything
    assert await checkpoints.latest(ConversationId(f"conv-{THEIRS}")) is None
    assert await sessions.get("login-C-1042") is None


@pytest.mark.discharges("AAC-0117")
async def test_the_ledger_is_reached_although_only_conversations_know_the_runs() -> None:
    """The ordering, pinned as the thing it is.

    Nothing outside the conversation rows can name this person's runs. If
    `forget` asked the ledger first, or erased conversations without taking the
    run ids back, this count would be zero and every other assertion here would
    still pass — which is how an erasure comes to report success and leave the
    ledger full.
    """
    checkpoints = InMemoryCheckpointStore()
    log = await ledger_for(await two_turns(checkpoints))

    erased = await erasure.forget(customer_id=THEIRS, checkpoints=checkpoints, requests=log)

    assert erased.answers == 2


@pytest.mark.discharges("AAC-0117")
async def test_a_person_with_nothing_held_is_not_an_error() -> None:
    """Asked about somebody the system never met. A refusal here would make the
    honest answer — *we hold nothing about you* — look like a fault."""
    erased = await erasure.forget(
        customer_id="C-9999",
        checkpoints=InMemoryCheckpointStore(),
        requests=req.InMemoryRequests(),
        sessions=InMemorySessionStore(),
        subject="login-nobody",
    )
    assert (erased.runs, erased.answers, erased.sessions) == (0, 0, 0)
    assert not erased.anything


@pytest.mark.discharges("AAC-0117")
async def test_a_login_already_logged_out_is_reported_as_none() -> None:
    """`delete` is idempotent and silent, so a report built from calling it
    would always claim one session went."""
    checkpoints = InMemoryCheckpointStore()
    await two_turns(checkpoints)

    erased = await erasure.forget(
        customer_id=THEIRS,
        checkpoints=checkpoints,
        sessions=InMemorySessionStore(),
        subject="login-C-1042",
    )
    assert erased.sessions == 0
    assert erased.runs == 2
