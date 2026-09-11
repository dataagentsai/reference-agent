"""AAC-0076 — triggers fire once and only once.

The last release gate with no test behind it. It is about the edge rather than
the agent: an agent that runs twice takes every action twice, correctly each
time, so nothing inside the run can catch it.

The third of the obligation — a trigger that never fires — is deliberately not
tested here. You cannot detect an absence from inside the thing that is absent.
It is covered at the other position by `tests/test_omission.py`, where a turn
that never arrives means a required effect never happens and the omission oracle
sees it. One idea, two positions, and the last test in this file pins that.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agenttwin import Live, load, omitted, project

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
PENDING = "AB-10002"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def cancels(times: int = 4) -> ScriptedClient:
    return ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),)
            ),
            ModelResponse(text="That has been cancelled."),
        ]
        * times
    )


# --------------------------------------------------------------------------- #
# The log.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0053", "AAC-0076")
async def test_a_settled_delivery_is_refused() -> None:
    log = trg.InMemoryDeliveryLog()
    async with trg.once(log, "msg-1"):
        pass

    with pytest.raises(trg.DuplicateDelivery):
        async with trg.once(log, "msg-1"):
            pass


@pytest.mark.discharges("AHC-0053", "AAC-0076")
async def test_an_in_flight_delivery_is_refused_differently() -> None:
    """Told apart from a duplicate because the operator response differs: a
    redelivery is routine, two concurrent turns for one conversation is a race."""
    log = trg.InMemoryDeliveryLog()
    async with trg.once(log, "msg-1"):
        with pytest.raises(trg.OverlappingRun):
            async with trg.once(log, "msg-1"):
                pass


@pytest.mark.discharges("AHC-0053", "AAC-0076")
async def test_a_failed_delivery_still_settles() -> None:
    """A delivery that was tried and failed has still been delivered. Re-running
    it on redelivery would repeat whatever effects it managed before failing."""
    log = trg.InMemoryDeliveryLog()
    with pytest.raises(RuntimeError):
        async with trg.once(log, "msg-1"):
            raise RuntimeError("the run blew up halfway")

    assert await log.state_of("msg-1") is trg.State.SETTLED
    with pytest.raises(trg.DuplicateDelivery):
        async with trg.once(log, "msg-1"):
            pass


@pytest.mark.discharges("AHC-0053")
async def test_different_deliveries_do_not_collide() -> None:
    log = trg.InMemoryDeliveryLog()
    for n in range(3):
        async with trg.once(log, f"msg-{n}"):
            pass
    assert await log.state_of("msg-2") is trg.State.SETTLED


async def test_an_unidentified_delivery_runs_unguarded() -> None:
    """`None` means the caller did not identify the delivery. Inventing an id
    here would produce a guard that can never fire and a green report with it."""
    log = trg.InMemoryDeliveryLog()
    for _ in range(3):
        async with trg.once(log, None):
            pass


def test_the_in_memory_log_says_it_is_not_durable() -> None:
    """It answers the obligation for one instance, not for a deployment, and it
    says so rather than letting a reader assume otherwise."""
    assert trg.InMemoryDeliveryLog.durable is False


# --------------------------------------------------------------------------- #
# End to end: the effect happens once.
# --------------------------------------------------------------------------- #


async def _turn(agent, world, text: str, delivery_id: str | None):
    return await agent.handle(text, identity=who(), delivery_id=delivery_id)


@pytest.mark.discharges("AAC-0076", "AHC-0053")
async def test_a_redelivered_message_does_not_cancel_twice() -> None:
    """The whole point, and the reason the ledger could not do it.

    Idempotency keys are `run + step + iteration`. A duplicate delivery produces
    a whole second run with its own key space, so the ledger never sees across
    the two — R-006 from the outside.
    """
    world = Live.start(load(WORLD))
    log = trg.InMemoryDeliveryLog()

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=cancels(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            deliveries=log,
        )
        await _turn(agent, world, f"cancel {PENDING}", "msg-7")
        with pytest.raises(trg.DuplicateDelivery):
            await _turn(agent, world, f"cancel {PENDING}", "msg-7")

    assert [e for e in world.effects if e[0] == "cancel_order"] == [("cancel_order", PENDING)]


async def test_without_a_delivery_id_the_second_run_acts_again() -> None:
    """The defect, pinned. Not a bug in the guard — a bug in *not using* it.

    Two deliveries with no id are two runs, two key spaces, and the ledger has
    nothing to compare. What stops a second cancellation here is the **world** —
    a cancelled order is no longer cancellable — and that is a control the
    business rules happened to provide, not one the harness supplied.

    Written as an assertion on the refusal rather than on the effect count so it
    says plainly which control did the stopping.
    """
    world = Live.start(load(WORLD))

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=cancels(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            deliveries=trg.InMemoryDeliveryLog(),
        )
        await _turn(agent, world, f"cancel {PENDING}", None)
        await _turn(agent, world, f"cancel {PENDING}", None)

    assert world.get("order", PENDING)["status"] == "cancelled"
    assert len([e for e in world.effects if e[0] == "cancel_order"]) == 1, (
        "the world refused the second one — the harness did not"
    )


@pytest.mark.discharges("AHC-0053", "AAC-0076")
async def test_two_concurrent_deliveries_do_not_both_run() -> None:
    """A queue with at-least-once semantics — which is every queue worth using —
    can deliver the same message to two workers at the same moment."""
    world = Live.start(load(WORLD))
    log = trg.InMemoryDeliveryLog()

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=cancels(), tools=tools, store=InMemoryCheckpointStore(), deliveries=log
        )
        outcomes = await asyncio.gather(
            _turn(agent, world, f"cancel {PENDING}", "msg-9"),
            _turn(agent, world, f"cancel {PENDING}", "msg-9"),
            return_exceptions=True,
        )

    refused = [o for o in outcomes if isinstance(o, trg.TriggerRefused)]
    assert len(refused) == 1, f"exactly one should be refused, got {outcomes}"
    assert len([e for e in world.effects if e[0] == "cancel_order"]) == 1


# --------------------------------------------------------------------------- #
# The third failure, at the other position.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_a_trigger_that_never_fires_is_an_omission() -> None:
    """The half this module cannot answer, pinned where it *is* answered.

    You cannot detect an absence from inside the thing that is absent. So the
    missed window is not a trigger check at all — it is the omission oracle
    seeing that a required effect never happened, which is the same idea one
    position over.
    """
    world = Live.start(load(WORLD))
    world.rows["order"]["AB-10009"] = {
        "id": "AB-10009",
        "customer_id": "C-1042",
        "status": "returned",
        "days_since_delivery": 12,
        "final_sale": False,
    }
    world_0 = world.snapshot()

    # No turn arrives at all. Nothing runs, nothing errors, nothing is logged.
    assert world.effects == []
    (missed,) = omitted(world, world_0)
    assert "issue_refund" in str(missed)
