"""An approval that waits too long says so, once, before it expires (T-059).

The workflow holds the clock, so the workflow is what notices — the agent's
turn ended when it asked, and nobody is watching a timer that runs for a day.
Against real workflows on the time-skipping server, where *an hour before it
expires* is the workflow's own timer rather than a number in a test.

What the rows are about: a reminder is sent when nobody has come, and is not
sent when somebody has, because a desk that is chased about work it has already
done stops reading what it is chased with.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from agenttwin import Live, load, project
from evals import durable
from tests.test_watching import WORLD

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Approval, ApprovalState, IdempotencyKey, Identity, RunId
from support_agent.idempotency import InMemoryLedger
from support_agent.tools import connect

CUSTOMER = "C-1042"
ORDER = "AB-10003"
TTL = 3600
REMIND_BEFORE = 900


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


async def acting_for(customer_id: str) -> Identity:
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


class Heard:
    """A notifier that writes down what it was asked to announce."""

    def __init__(self) -> None:
        self.told: list[Approval] = []

    async def waiting(self, approval: Approval) -> None:
        self.told.append(approval)


@asynccontextmanager
async def waiting_for(policy: ap.Policy):
    """A refund that needs a person, on a clock a test can move."""
    heard = Heard()
    world = Live.start(load(WORLD))
    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        durable.approvals_for(tools, acting_for=acting_for, policy=policy, notifier=heard) as waits,
    ):
        yield waits, heard


async def ask_for_a_refund(waits: Any) -> str:
    key = IdempotencyKey(run_id=RunId(f"run_r_{int(time.time() * 1000)}"), step=0, iteration=0)
    raised = await waits.approvals.request(
        action=ap.REFUND_ACTION,
        args={"order_id": ORDER},
        identity=Identity(customer_id=CUSTOMER, scopes=ident.CUSTOMER_SCOPES),
        idempotency_key=key,
        conversation_id="cw-1-77",
    )
    assert raised.state is ApprovalState.WAITING, raised.state
    return str(raised.id)


POLICY = ap.Policy(ttl_s=TTL, remind_before_s=REMIND_BEFORE)

# (why, how far the clock moves, how many reminders by then)
CLOCK = [
    ("nothing is said while there is time", TTL - REMIND_BEFORE - 60, 0),
    ("an hour before it expires, somebody is told", TTL - REMIND_BEFORE + 60, 1),
    ("and told once, not once a minute", TTL - 60, 1),
]


@pytest.mark.parametrize(("why", "moves", "reminders"), CLOCK, ids=[r[0] for r in CLOCK])
@pytest.mark.discharges("AAC-0043", "P-APPROVAL-TTL")
async def test_a_wait_that_is_running_out_says_so(why: str, moves: int, reminders: int) -> None:
    async with waiting_for(POLICY) as (waits, heard):
        await ask_for_a_refund(waits)
        await waits.env.sleep(moves)

    assert len(heard.told) == reminders
    if reminders:
        told = heard.told[0]
        assert told.state is ApprovalState.WAITING, "it is announced while it can still be decided"
        assert told.conversation_id == "cw-1-77", "addressed to where it came from"


@pytest.mark.discharges("AAC-0043")
async def test_a_decided_approval_chases_nobody() -> None:
    """A desk chased about work it has already done stops reading what it is
    chased with."""
    async with waiting_for(POLICY) as (waits, heard):
        approval_id = await ask_for_a_refund(waits)
        await ap.ApprovalDesk(waits.env.client).decide(approval_id, granted=False, by="desk-1")
        await waits.env.sleep(TTL + 60)

    assert heard.told == []


@pytest.mark.discharges("AAC-0043")
async def test_no_reminder_is_asked_for_when_there_is_nowhere_to_send_one() -> None:
    """`remind_before_s = 0` is a deployment with no channel saying so, rather
    than a notifier quietly dropping what it was handed."""
    async with waiting_for(ap.Policy(ttl_s=TTL, remind_before_s=0)) as (waits, heard):
        await ask_for_a_refund(waits)
        await waits.env.sleep(TTL - 60)

    assert heard.told == []


@pytest.mark.discharges("AAC-0009")
async def test_a_reminder_nobody_can_deliver_does_not_disturb_the_wait() -> None:
    """The wait is the record and the notification is a courtesy on top of it,
    so a notifier that raises must not expire an approval early or fail it."""

    class Broken:
        async def waiting(self, approval: Approval) -> None:
            raise RuntimeError("the inbox is down")

    world = Live.start(load(WORLD))
    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        durable.approvals_for(
            tools, acting_for=acting_for, policy=POLICY, notifier=Broken()
        ) as waits,
    ):
        approval_id = await ask_for_a_refund(waits)
        await waits.env.sleep(TTL - REMIND_BEFORE + 60)
        still = await waits.approvals.get(approval_id)
        assert still is not None
        assert still.state is ApprovalState.WAITING, "the wait is untouched by a failed reminder"

        await ap.ApprovalDesk(waits.env.client).decide(approval_id, granted=False, by="desk-1")
        decided = await waits.approvals.get(approval_id)
        assert decided is not None
        assert decided.state is ApprovalState.REFUSED, "and it can still be decided"
