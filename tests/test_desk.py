"""The desk as an actor — F-007, one record along.

`tests/test_escalation.py` drives the escalation queue directly: raise a record,
reach into the store, assert. That exercises the queue. It cannot express a desk
that takes twenty minutes, disagrees that the escalation was needed, walks away,
or arrives after the customer has already been told nobody came — and those are
the four things a real one does.

The scenarios below run the whole path: a customer asks for a person, the agent
raises a record and stops serving, a desk works the queue between turns, and the
customer comes back to find out what happened.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from agenttwin import (
    Clock,
    Desk,
    Live,
    Rule,
    Scenario,
    ScriptedActor,
    StateMachineActor,
    load,
    project,
    run_scenario,
)
from evals import durable

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import EscalationState, Identity, ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
ORDER = "AB-10001"
MINUTE = 60
HOUR = 3600


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def patient() -> ScriptedClient:
    """The agent never reaches the model on an escalated turn, so these exist
    only for the turns after a handoff has been given back.

    It used to say *"Let me look that up."* — an unbacked promise, which
    AHC-0106 now escalates. That made every test using this fixture fetch a
    person for a reason none of them was about, and one of them (the frustrated
    customer below) started passing for the wrong reason. The filler says
    something true and finished instead, so each test measures what it claims to.
    """
    return ScriptedClient([ModelResponse(text="I am sorry about that.")] * 6)


@asynccontextmanager
async def conversation(factory, *, said=None, turns: int = 3, step_s: int = MINUTE, **desk_kw):
    """A customer who asks for a person, then follows up, while a desk does or
    does not pick the escalation up.

    The escalation is a Temporal workflow on the test server, on the scenario's
    clock (T-028), so "arrives too late" is the workflow's lapse timer rather
    than a row somebody rewrote.
    """
    world = Live.start(load(WORLD))
    clock = Clock(step_s=step_s)
    scenario = Scenario(name="a customer who wants a person", max_turns=turns)
    actor = ScriptedActor((said or ["I want to speak to a human", "any update?", "hello?"])[:turns])

    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        durable.escalations_for(clock=clock) as waits,
    ):
        desk = factory(waits.escalations, durable.resolving(waits.colleagues), **desk_kw)
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=waits.escalations,
            clock=clock,
        )
        record = await run_scenario(
            scenario,
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=clock,
        )
        yield world, record, desk, waits.escalations


# --------------------------------------------------------------------------- #
# The four desks. What each one leaves behind is the point.
# --------------------------------------------------------------------------- #

# (name, which desk, seconds a turn, how long it takes, its record, the row)
DESKS = [
    ("picks it up", Desk.answers, MINUTE, 0, "resolved", EscalationState.RESOLVED),
    (
        "says it was not needed",
        Desk.says_agent_could_have,
        MINUTE,
        0,
        "agent_could_have",
        EscalationState.RESOLVED,
    ),
    ("never comes", Desk.never_comes, MINUTE, 0, "waiting", EscalationState.QUEUED),
    # Forty minutes to answer, against a thirty-minute window: the workflow's
    # timer lapses it first, and the desk that turns up afterwards is refused.
    (
        "arrives too late",
        Desk.answers,
        20 * MINUTE,
        40 * MINUTE,
        "refused",
        EscalationState.EXPIRED,
    ),
]


@pytest.mark.parametrize(
    ("name", "factory", "step_s", "delay_s", "outcome", "state"), DESKS, ids=[c[0] for c in DESKS]
)
@pytest.mark.discharges("ext:escalation_desk", "op:escalate")
async def test_what_each_desk_leaves_behind(
    name: str, factory, step_s: int, delay_s: int, outcome: str, state: EscalationState
) -> None:
    """Four desks, four different rows. `arrives too late` takes longer to
    answer than the escalation's window allows, so it finds the workflow has
    already lapsed it and is refused — a third outcome, distinct from answering
    and from silence, and the one most likely to be mishandled."""
    async with conversation(factory, step_s=step_s, delay_s=delay_s) as (
        _world,
        _record,
        desk,
        store,
    ):
        # The last word, because a desk that takes its time says "waiting"
        # first and the outcome is what it settles on.
        assert desk.handled[-1].outcome == outcome, [str(h) for h in desk.handled]
        raised = await store.get(desk.handled[0].escalation_id)

    assert raised is not None
    assert raised.state is state


@pytest.mark.discharges("ext:escalation_desk", "op:escalate")
async def test_a_handled_escalation_returns_the_customer_to_the_agent() -> None:
    """The round trip. The agent stops serving, the desk closes it, and the
    customer is served again — without anybody telling the agent directly."""
    async with conversation(
        Desk.answers,
        said=["I want to speak to a human", f"where is my order {ORDER}"],
        turns=2,
    ) as (_world, _record, desk, store):
        assert {h.outcome for h in desk.handled} == {"resolved"}
        closed = await store.get(desk.handled[0].escalation_id)

    assert closed is not None
    assert closed.state is EscalationState.RESOLVED
    assert closed.outcome is not None and closed.outcome.value == "resolved"


@pytest.mark.discharges("P-ESC-OUTCOME")
async def test_the_over_escalation_label_reaches_the_row() -> None:
    """The number that makes over-escalation measurable rather than arguable.

    A desk that only ever agrees with us tests nothing: the false-positive rate
    is supplied by the person who picked the ticket up, and this is the only
    place it can enter the system.
    """
    async with conversation(Desk.says_agent_could_have, turns=2, note="ordinary order status") as (
        _world,
        _record,
        desk,
        store,
    ):
        closed = await store.get(desk.handled[0].escalation_id)

    assert closed is not None
    assert closed.outcome is not None and closed.outcome.value == "agent_could_have"
    assert closed.outcome_by == "desk-3"
    assert closed.outcome_note == "ordinary order status"
    assert closed.rule_id == "asked-for-human", "sliceable by the rule that raised it"


@pytest.mark.discharges("P-ESC-OWNS")
async def test_a_desk_cannot_close_its_own_customers_escalation() -> None:
    """The confused deputy of the human path, on the escalation record.

    A customer who could mark their own case resolved would make the
    over-escalation rate a number written by the party it measures.
    """
    async with conversation(Desk.answers, turns=2, name="C-1042") as (
        _world,
        _record,
        desk,
        store,
    ):
        still_open = await store.pending()

    assert {h.outcome for h in desk.handled} == {"refused"}
    assert all("customer" in h.detail for h in desk.handled)
    assert len(still_open) == 1, "a refused close must not quietly resolve it"


@pytest.mark.discharges("P-ESC-TTL", "P-ESC-LAPSE")
async def test_nobody_comes_and_the_queue_says_so() -> None:
    """Nothing happens, which is correct — and the point is that nothing happens
    *quietly*. The escalation is still sitting there after the customer has given
    up, and no part of the system says so."""
    async with conversation(Desk.never_comes) as (_world, _record, desk, store):
        queued = await store.pending()

    assert {h.outcome for h in desk.handled} == {"waiting"}
    assert queued, "still queued, and nobody is alerted"


# --------------------------------------------------------------------------- #
# What AgentTwin can now see that we cannot yet do. These pass by asserting the
# gap, and are the tests that should start failing when Tier 2 lands.
# --------------------------------------------------------------------------- #


@pytest.mark.documents_gap(
    "dissatisfaction is not an intent, so a customer repeating it carries none to "
    "repeat and no rule sees them (GAPS section 7)"
)
async def test_a_frustrated_customer_is_never_escalated_today() -> None:
    """The gap `repeated-intent` does **not** close — narrower now, and named.

    A customer who says "that is not good enough" four times has, by any
    operational standard, earned a person. Nothing notices: dissatisfaction is
    not an intent, so the router classifies none, and a fact about *the same
    intent repeated* cannot count turns that carry no intent at all.

    This test was written expecting `repeated-intent` to make it fail. It ships
    (F-025) and this still passes, which is the honest answer: a customer asking
    the same thing three times now reaches a person
    (`test_a_customer_asking_the_same_thing_three_times_reaches_a_person`), and a
    customer who is merely unhappy does not. What would cover it is the AOAS's
    deferred trigger *three failed resolution attempts*, and "failed" there needs
    a definition nobody has written — recorded as a gap rather than guessed at.
    """
    world = Live.start(load(WORLD))
    clock = Clock(step_s=MINUTE)
    actor = StateMachineActor(
        opening="where is my order AB-10001",
        rules=[Rule(when=re.compile(r".*", re.S), say="that is not good enough")],
        max_turns=4,
        persistence="that is not good enough",
    )

    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        durable.escalations_for(clock=clock) as waits,
    ):
        desk = Desk.answers(waits.escalations, durable.resolving(waits.colleagues))
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=waits.escalations,
            clock=clock,
        )
        await run_scenario(
            Scenario(name="a customer nobody helps", max_turns=4),
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=clock,
        )
        queued = await waits.escalations.pending()

    assert queued == ()
    assert desk.handled == [], "no escalation was ever raised, so the desk saw nothing"


@pytest.mark.discharges("op:escalate", "ext:escalation_desk")
async def test_the_clock_drives_expiry_rather_than_a_rewritten_row() -> None:
    """`contracts.Clock`, finally implemented by something.

    Here the scenario's clock is the agent's clock, and the escalation
    workflow's. Nobody touches the record — time simply passes, the workflow's
    own timer lapses it, and the customer is told the truth about it.
    """
    ticking = Clock(step_s=20 * MINUTE)
    world = Live.start(load(WORLD))
    # Three turns: the window closes during the second, and the third is when the
    # agent looks and finds nothing came. Time moves on a tick per turn now and
    # not on every read, so the turn that observes the lapse has to exist.
    actor = ScriptedActor(["I want to speak to a human", "any update?", "anyone?"])

    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        durable.escalations_for(clock=ticking) as waits,
    ):
        desk = Desk.never_comes(waits.escalations, durable.resolving(waits.colleagues))
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=waits.escalations,
            clock=ticking,
        )
        record = await run_scenario(
            Scenario(name="nobody ever came", max_turns=3),
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=ticking,
        )
        raised = desk.handled[0].escalation_id
        lapsed = await waits.escalations.get(raised)
        queued = await waits.escalations.pending()

    assert lapsed is not None
    assert lapsed.state is EscalationState.EXPIRED, "the window passed and nobody came"
    assert raised in record.reply, "the customer is told which reference lapsed"
    assert queued == ()
